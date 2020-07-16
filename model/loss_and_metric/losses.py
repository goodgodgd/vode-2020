import tensorflow as tf
from tensorflow.keras import layers
from model.synthesize.synthesize_base import SynthesizeMultiScale
from model.synthesize.flow_warping import FlowWarpMultiScale

import utils.util_funcs as uf
from utils.util_class import WrongInputException
import utils.convert_pose as cp
from utils.decorators import shape_check
from model.loss_and_metric.loss_util import photometric_loss_l1, photometric_loss_ssim


class TotalLoss:
    def __init__(self, calc_losses=None, loss_weights=None, stereo=False):
        """
        :param calc_losses: list of loss calculators
        :param loss_weights: list of weights of loss calculators
        """
        self.calc_losses = calc_losses
        self.loss_weights = loss_weights
        self.stereo = stereo

    @shape_check
    def __call__(self, predictions, features):
        """
        :param predictions: {"depth_ms": .., "disp_ms": .., "pose": ..}
            disp_ms: multi scale disparity, list of [batch, height/scale, width/scale, 1]
            pose: 6-DoF poses [batch, num_src, 6]
        :param features: {"image": .., "pose_gt": .., "depth_gt": .., "intrinsic": ..}
            image: stacked image [batch, height*snippet_len, width, 3]
            intrinsic: camera projection matrix [batch, 3, 3]
        :return loss: final loss of frames in batch (scalar)
                losses: list of losses computed from calc_losses
        """
        augm_data = self.augment_data(features, predictions)
        if self.stereo:
            augm_data_rig = self.augment_data(features, predictions, "_R")
            augm_data.update(augm_data_rig)

        losses = []
        for calc_loss, weight in zip(self.calc_losses, self.loss_weights):
            loss = calc_loss(features, predictions, augm_data)
            losses.append(loss * weight)

        losses = layers.Lambda(lambda x: tf.stack(x, axis=0), name="losses")(losses)
        total_loss = layers.Lambda(lambda x: tf.reduce_sum(x), name="total_loss")(losses)
        return total_loss, losses

    def augment_data(self, features, predictions, suffix=""):
        """
        gather additional data required to compute losses
        :param features: {image, intrinsic}
                image: stacked image snippet [batch, snippet_len*height, width, 3]
                intrinsic: camera projection matrix [batch, 3, 3]
        :param predictions: {disp_ms, pose}
                disp_ms: multi scale disparities, list of [batch, height/scale, width/scale, 1]
                pose: poses that transform points from target to source [batch, num_src, 6]
        :param suffix: suffix to keys
        :return augm_data: {depth_ms, source, target, target_ms, synth_target_ms}
                depth_ms: multi scale depth, list of [batch, height/scale, width/scale, 1]
                source: source frames [batch, num_src*height, width, 3]
                target: target frame [batch, height, width, 3]
                target_ms: multi scale target frame, list of [batch, height/scale, width/scale, 3]
                synth_target_ms: multi scale synthesized target frames generated from each source image,
                                list of [batch, num_src, height/scale, width/scale, 3]
                warped_target_ms: multi scale flow warped target frames generated from each source image,
                                list of [batch, num_src, height/scale, width/scale, 3]
        """
        augm_data = dict()
        pred_depth_ms = predictions["depth_ms" + suffix]
        pred_pose = predictions["pose" + suffix]

        stacked_image = features["image" + suffix]
        intrinsic = features["intrinsic" + suffix]
        source_image, target_image = uf.split_into_source_and_target(stacked_image)
        target_ms = uf.multi_scale_like_depth(target_image, pred_depth_ms)
        augm_data["source" + suffix] = source_image
        augm_data["target" + suffix] = target_image
        augm_data["target_ms" + suffix] = target_ms
        # synthesized image is used in both L1 and SSIM photometric losses
        synth_target_ms = SynthesizeMultiScale()(source_image, intrinsic, pred_depth_ms, pred_pose)
        augm_data["synth_target_ms" + suffix] = synth_target_ms

        # warped image is used in both L1 and SSIM photometric losses
        if "flow_ms" + suffix in predictions:
            pred_flow_ms = predictions["flow_ms" + suffix]
            flow_target_ms = uf.multi_scale_like_flow(target_image, pred_flow_ms)
            augm_data["flow_target_ms" + suffix] = flow_target_ms
            warped_target_ms = FlowWarpMultiScale()(source_image, pred_flow_ms)
            augm_data["warped_target_ms" + suffix] = warped_target_ms
        return augm_data


class LossBase:
    def __call__(self, features, predictions, augm_data):
        raise NotImplementedError()


class PhotometricLoss(LossBase):
    def __init__(self, method, key_suffix=""):
        if method == "L1":
            self.photometric_loss = photometric_loss_l1
        elif method == "SSIM":
            self.photometric_loss = photometric_loss_ssim
        else:
            raise WrongInputException("Wrong photometric loss name: " + method)

        self.key_suffix = key_suffix

    def __call__(self, features, predictions, augm_data):
        raise NotImplementedError()


class PhotometricLossMultiScale(PhotometricLoss):
    def __init__(self, method, key_suffix=""):
        super().__init__(method, key_suffix)

    def __call__(self, features, predictions, augm_data):
        """
        desciptions of inputs are available in 'TotalLoss.augment_data()'
        :return: photo_loss (scalar)
        """
        original_target_ms = augm_data["target_ms" + self.key_suffix]
        synth_target_ms = augm_data["synth_target_ms" + self.key_suffix]

        losses = []
        for i, (synt_target, orig_target) in enumerate(zip(synth_target_ms, original_target_ms)):
            loss = layers.Lambda(lambda inputs: self.photometric_loss(inputs[0], inputs[1]),
                                 name=f"photo_loss_{i}" + self.key_suffix)([synt_target, orig_target])
            losses.append(loss)
        # losses: list of scale losses -> sum: scalar
        name = "photo_loss_sum" + self.key_suffix
        loss = layers.Lambda(lambda x: tf.reduce_sum(x), name=name)(losses)
        return loss


class SmoothenessLossMultiScale(LossBase):
    def __init__(self, key_suffix=""):
        self.key_suffix = key_suffix

    def __call__(self, features, predictions, augm_data):
        """
        desciptions of inputs are available in "PhotometricLossL1MultiScale"
        :return: smootheness loss (scalar)
        """
        pred_disp_ms = predictions["disp_ms" + self.key_suffix]
        target_ms = augm_data["target_ms" + self.key_suffix]
        losses = []
        orig_width = target_ms[0].get_shape().as_list()[2]
        for i, (disp, image) in enumerate(zip(pred_disp_ms, target_ms)):
            scale = orig_width / image.get_shape().as_list()[2]
            loss = layers.Lambda(lambda inputs: self.smootheness_loss(inputs[0], inputs[1]) / scale,
                                 name=f"smooth_loss_{i}")([disp, image])
            losses.append(loss)

        loss = layers.Lambda(lambda x: tf.reduce_sum(x), name="smooth_loss_sum")(losses)
        return loss

    def smootheness_loss(self, disp, image):
        """
        :param disp: scaled disparity map, list of [batch, height/scale, width/scale, 1]
        :param image: scaled original target image [batch, height/scale, width/scale, 3]
        :return: smootheness loss (scalar)
        """
        def gradient_x(img):
            gx = img[:, :, :-1, :] - img[:, :, 1:, :]
            return gx

        def gradient_y(img):
            gy = img[:, :-1, :, :] - img[:, 1:, :, :]
            return gy

        disp_gradients_x = gradient_x(disp)
        disp_gradients_y = gradient_y(disp)

        image_gradients_x = gradient_x(image)
        image_gradients_y = gradient_y(image)

        weights_x = tf.exp(-tf.reduce_mean(tf.abs(image_gradients_x), 3, keepdims=True))
        weights_y = tf.exp(-tf.reduce_mean(tf.abs(image_gradients_y), 3, keepdims=True))

        # [batch, height/scale, width/scale, 1]
        smoothness_x = disp_gradients_x * weights_x
        smoothness_y = disp_gradients_y * weights_y

        smoothness_x = 0.5 * tf.reduce_mean(tf.abs(smoothness_x))
        smoothness_y = 0.5 * tf.reduce_mean(tf.abs(smoothness_y))
        smoothness = smoothness_x + smoothness_y
        return smoothness


class StereoDepthLoss(PhotometricLoss):
    def __init__(self, method):
        super().__init__(method)

    def __call__(self, features, predictions, augm_data):
        """
        desciptions of inputs are available in 'TotalLoss.augment_data()'
        :return: photo_loss (scalar)
        """
        # synthesize left image from right image
        loss_left = self.stereo_synthesize_loss(source_img=augm_data["target_R"],
                                                target_ms=augm_data["target_ms"],
                                                target_depth_ms=predictions["depth_ms"],
                                                pose_t2s=tf.linalg.inv(features["stereo_T_LR"]),
                                                intrinsic=features["intrinsic"])
        # synthesize right image from left image
        loss_right = self.stereo_synthesize_loss(source_img=augm_data["target"],
                                                 target_ms=augm_data["target_ms_R"],
                                                 target_depth_ms=predictions["depth_ms_R"],
                                                 pose_t2s=features["stereo_T_LR"],
                                                 intrinsic=features["intrinsic_R"],
                                                 suffix="_R")
        # list concatenation, not summation
        losses = loss_left + loss_right
        # losses: [loss at each scale] -> sum: scalar
        loss = layers.Lambda(lambda x: tf.reduce_sum(x), name="photo_loss_sum")(losses)
        return loss

    def stereo_synthesize_loss(self, source_img, target_ms, target_depth_ms, pose_t2s, intrinsic, suffix=""):
        """
        synthesize image from source to target
        :param source_img: [batch, num_src*height, width, 3]
        :param target_ms: list of [batch, height/scale, width/scale, 3]
        :param target_depth_ms: list of [batch, height/scale, width/scale, 1]
        :param pose_t2s: [batch, num_src, 4, 4]
        :param intrinsic: [batch, num_src, 3, 3]
        :param suffix: "" if right to left, else "_R"
        :return losses [scales]
        """
        pose_stereo = cp.pose_matr2rvec_batch(tf.expand_dims(pose_t2s, 1))

        # synth_target_ms: list of [batch, 1, height/scale, width/scale, 3]
        synth_target_ms = SynthesizeMultiScale()(source_img, intrinsic, target_depth_ms, pose_stereo)
        losses = []
        for i, (synth_img_sc, target_img_sc) in enumerate(zip(synth_target_ms, target_ms)):
            loss = layers.Lambda(lambda inputs: self.photometric_loss(inputs[0], inputs[1]),
                                 name=f"photo_loss_{i}" + suffix)([synth_img_sc, target_img_sc])
            losses.append(loss)
        return losses


class StereoPoseLoss(LossBase):
    def __call__(self, features, predictions, augm_data):
        pose_lr_pred = predictions["pose_LR"]
        pose_rl_pred = predictions["pose_RL"]
        pose_lr_true_mat = features["stereo_T_LR"]
        pose_lr_true_mat = tf.expand_dims(pose_lr_true_mat, axis=1)
        pose_rl_true_mat = tf.linalg.inv(pose_lr_true_mat)
        pose_lr_true = cp.pose_matr2rvec_batch(pose_lr_true_mat)
        pose_rl_true = cp.pose_matr2rvec_batch(pose_rl_true_mat)
        # loss: [batch, num_src]
        loss = tf.keras.losses.MSE(pose_lr_true, pose_lr_pred) + tf.keras.losses.MSE(pose_rl_true, pose_rl_pred)
        # loss: (scalar)
        loss = tf.reduce_mean(loss)
        return loss


class FlowWarpLossMultiScale(PhotometricLoss):
    def __init__(self, method, key_suffix=""):
        super().__init__(method, key_suffix)

    def __call__(self, features, predictions, augm_data):
        """
        desciptions of inputs are available in 'TotalLoss.augment_data()'
        :return: photo_loss (scalar)
        """
        flow_target_ms = augm_data["flow_target_ms" + self.key_suffix]
        # warp a target from 4 sources and 4 flows in 4 level scales
        warped_target_ms = augm_data["warped_target_ms" + self.key_suffix]

        losses = []
        for i, (warp_target, orig_target) in enumerate(zip(warped_target_ms, flow_target_ms)):
            loss = layers.Lambda(lambda inputs: self.photometric_loss(inputs[0], inputs[1]),
                                 name=f"flow_warp_loss_{i}" + self.key_suffix)([warp_target, orig_target])
            losses.append(loss)
        # losses: [loss at each scale] -> sum: scalar
        name = "flow_warp_loss_sum" + self.key_suffix
        loss = layers.Lambda(lambda x: tf.reduce_sum(x), name=name)(losses)
        return loss


class L2Regularizer(LossBase):
    def __init__(self, weights_to_regularize):
        self.weights = weights_to_regularize

    def __call__(self, features, predictions, augm_data):
        loss = 0
        for weight in self.weights:
            loss += tf.nn.l2_loss(weight)
        return loss


# ===== TEST FUNCTIONS

import numpy as np


def test_average_pool_3d():
    print("\n===== start test_average_pool_3d")
    ksize = [1, 3, 3]
    average_pool = tf.keras.layers.AveragePooling3D(pool_size=ksize, strides=1, padding="SAME")
    for i in range(10):
        x = tf.random.normal((8, 4, 100, 100, 3))
        y = tf.random.normal((8, 4, 100, 100, 3))
        mu_x = average_pool(x)
        mu_y = average_pool(y)
        npx = x.numpy()
        npy = y.numpy()
        npmux = mu_x.numpy()
        npmuy = mu_y.numpy()
        print(i, "mean x", npx[0, 0, 10:13, 10:13, 1].mean(), npmux[0, 0, 11, 11, 1],
              "mean y", npy[0, 0, 10:13, 10:13, 1].mean(), npmuy[0, 0, 11, 11, 1])
        assert np.isclose(npx[0, 0, 10:13, 10:13, 1].mean(), npmux[0, 0, 11, 11, 1])
        assert np.isclose(npy[0, 0, 10:13, 10:13, 1].mean(), npmuy[0, 0, 11, 11, 1])

    print("!!! test_average_pool_3d passed")


# in this function, "tf.nn.avg_pool()" works fine with gradient tape in eager mode
def test_gradient_tape():
    print("\n===== start test_gradient_tape")
    out_dim = 10
    x = tf.cast(np.random.uniform(0, 1, (200, 100, 100, 3)), tf.float32)
    y = tf.cast(np.random.uniform(0, 1, (200, out_dim)), tf.float32)
    dataset = tf.data.Dataset.from_tensor_slices((x, y))
    dataset = dataset.shuffle(100).batch(8)

    input_layer = tf.keras.layers.Input(shape=(100, 100, 3))
    z = tf.keras.layers.Conv2D(64, 3)(input_layer)
    z = tf.keras.layers.Conv2D(out_dim, 3)(z)

    # EXECUTE
    z = tf.nn.avg_pool(z, ksize=[3, 3], strides=[1, 1], padding="SAME")
    z = tf.keras.layers.GlobalAveragePooling2D()(z)
    model = tf.keras.Model(inputs=input_layer, outputs=z)
    optimizer = tf.keras.optimizers.SGD()

    for i, (xi, yi) in enumerate(dataset):
        train_model(model, optimizer, xi, yi)
        uf.print_progress_status(f"optimizing... {i}")


def train_model(model, optimizer, xi, yi):
    with tf.GradientTape() as tape:
        yi_hat = model(xi)
        loss = tf.keras.losses.MeanSquaredError()(yi, yi_hat)

    grad = tape.gradient(loss, model.trainable_weights)
    optimizer.apply_gradients(zip(grad, model.trainable_weights))


if __name__ == "__main__":
    test_average_pool_3d()
    test_gradient_tape()
