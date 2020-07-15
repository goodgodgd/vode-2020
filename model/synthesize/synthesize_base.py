import numpy as np
import tensorflow as tf
from tensorflow.keras import layers

from utils.decorators import shape_check
from model.synthesize.bilinear_interp import BilinearInterpolation
from utils.convert_pose import pose_rvec2matr_batch


class SynthesizeMultiScale:
    @shape_check
    def __call__(self, src_img_stacked, intrinsic, pred_depth_ms, pred_pose):
        """
        :param src_img_stacked: source images stacked vertically [batch, height*num_src, width, 3]
        :param intrinsic: [batch, 3, 3]
        :param pred_depth_ms: predicted target depth in multi scale, list of [batch, height/scale, width/scale, 1]}
        :param pred_pose: predicted source pose in twist vector for each source frame [batch, num_src, 6]
                        it transforms target points to source frame
        :return: reconstructed target view in multi scale, list of [batch, num_src, height/scale, width/scale, 3]}
        """
        # convert pose vector to transformation matrix
        poses_matr = layers.Lambda(lambda pose: pose_rvec2matr_batch(pose),
                                   name="pose2matrix")(pred_pose)
        synth_targets = []
        for depth_sc in pred_depth_ms:
            synth_target_sc = SynthesizeBatchBasic()(src_img_stacked, intrinsic, depth_sc, poses_matr)
            synth_targets.append(synth_target_sc)

        return synth_targets


class SynthesizeBatchBasic:
    def __init__(self, shape=(0, 0, 0), num_src=0, scale=0):
        # shape is scaled from the original shape, height = original_height / scale
        self.batch, self.height, self.width = shape
        self.num_src = num_src
        self.scale = scale

    def __call__(self, src_img_stacked, intrinsic, depth_sc, poses_matr):
        """
        :param src_img_stacked: stacked source images [batch, height*num_src, width, 3]
        :param intrinsic: intrinsic parameters [batch, 3, 3]
        :param depth_sc: scaled predicted depth for target image, [batch, height/scale, width/scale, 1]
        :param poses_matr: predicted source pose in matrix form [batch, num_src, 4, 4]
        :return: reconstructed target view in scale, [batch, num_src, height/scale, width/scale, 3]
        """
        suffix = f"_sc{self.scale}"
        self.read_shape(src_img_stacked, depth_sc)
        # adjust intrinsic upto scale
        intrinsic_sc = layers.Lambda(lambda intrin: self.scale_intrinsic(intrin, self.scale),
                                     name=f"scale_intrin" + suffix)(intrinsic)
        # reorganize and resize source images: [batch, 4, height/scale, width/scale, 3]
        source_images_sc = layers.Lambda(lambda image: self.reshape_source_images(image),
                                         name=f"reorder_source" + suffix)(src_img_stacked)
        # reconstruct target view from source images
        recon_image_sc = self.synthesize_batch_view(source_images_sc, depth_sc, poses_matr,
                                                    intrinsic_sc, suffix=f"sc{self.scale}")
        return recon_image_sc

    @shape_check
    def read_shape(self, src_img_stacked, depth_sc):
        batch_size, stacked_height, width_orig, _ = src_img_stacked.get_shape().as_list()
        self.batch, self.height, self.width, _ = depth_sc.get_shape().as_list()
        self.scale = int(width_orig / self.width)
        self.num_src = int(stacked_height / self.scale / self.height)

    def scale_intrinsic(self, intrinsic, scale):
        scaled_part = tf.slice(intrinsic, (0, 0, 0), (-1, 2, -1))
        scaled_part = scaled_part / scale
        const_part = tf.tile(tf.constant([[[0, 0, 1]]], dtype=tf.float32), (self.batch, 1, 1))
        scaled_intrinsic = tf.concat([scaled_part, const_part], axis=1)
        return scaled_intrinsic

    @shape_check
    def reshape_source_images(self, src_img_stacked):
        """
        :param src_img_stacked: [batch, height*num_src, width, 3]
        :return: reorganized source images [batch, num_src, height/scale, width/scale, 3]
        """
        batch_size, stacked_height, width_orig, _ = src_img_stacked.get_shape()
        height_orig = stacked_height // self.num_src
        # reshape image -> (batch*num_src, height_orig, width_orig, 3)
        source_images = tf.reshape(src_img_stacked, shape=(self.batch * self.num_src, height_orig, width_orig, 3))
        # resize image (scaled) -> (batch*num_src, height, width, 3)
        scaled_image = tf.image.resize(source_images, size=(self.height, self.width), method="bilinear")
        # reorganize scaled images -> (batch, num_src, height, width, 3)
        source_images = tf.reshape(scaled_image, shape=(self.batch, self.num_src, self.height, self.width, 3))
        return source_images

    @shape_check
    def synthesize_batch_view(self, src_image, tgt_depth, pose, intrinsic, suffix):
        """
        src_image, tgt_depth and intrinsic are scaled
        :param src_image: source image nearby the target image [batch, num_src, height, width, 3]
        :param tgt_depth: depth map of the target image in meter scale [batch, height, width, 1]
        :param pose: pose matrices that transform points from target to source frame [batch, num_src, 4, 4]
        :param intrinsic: camera projection matrix [batch, 3, 3]
        :param suffix: suffix to tensor name
        :return: synthesized target image [batch, num_src, height, width, 3]
        """
        src_pixel_coords = layers.Lambda(lambda inputs: self.warp_pixel_coords(inputs, self.height, self.width),
                                         name="warp_pixel_" + suffix)([tgt_depth, pose, intrinsic])
        tgt_image_synthesized = layers.Lambda(lambda inputs:
                                              BilinearInterpolation()(inputs[0], inputs[1], inputs[2]),
                                              name="recon_interp_" + suffix)(
                                              [src_pixel_coords, src_image, tgt_depth])
        return tgt_image_synthesized

    def warp_pixel_coords(self, inputs, height, width):
        tgt_depth, pose, intrinsic = inputs
        tgt_pixel_coords = self.pixel_meshgrid(height, width)
        tgt_cam_coords = self.pixel2cam(tgt_pixel_coords, tgt_depth, intrinsic)
        src_cam_coords = self.transform_to_source(tgt_cam_coords, pose)
        src_pixel_coords = self.cam2pixel(src_cam_coords, intrinsic)
        return src_pixel_coords

    def pixel_meshgrid(self, height, width, stride=1):
        """
        :return: pixel coordinates like vectors of (u,v,1) [3, height*width]
        """
        v = np.linspace(0, height - stride, int(height // stride)).astype(np.float32)
        u = np.linspace(0, width - stride, int(width // stride)).astype(np.float32)
        ugrid, vgrid = tf.meshgrid(u, v)
        uv = tf.stack([ugrid, vgrid], axis=0)
        uv = tf.reshape(uv, (2, -1))
        uv = tf.concat([uv, tf.ones((1, height*width), tf.float32)], axis=0)
        return uv

    def pixel2cam(self, pixel_coords, depth, intrinsic):
        """
        :param pixel_coords: (u,v,1) [3, height*width]
        :param depth: [batch, height, width, 1]
        :param intrinsic: [batch, 3, 3]
        :return: 3D points like (x,y,z,1) in target frame [batch, 4, height*width]
        """
        depth = tf.reshape(depth, (self.batch, 1, -1))

        # calc sum of products over specified dimension
        # cam_coords[i, j, k] = inv(intrinsic)[i, j, :] dot pixel_coords[:, k]
        # [batch, 3, height*width] = [batch, 3, 3] x [3, height*width]
        cam_coords = tf.tensordot(tf.linalg.inv(intrinsic), pixel_coords, [[2], [0]])

        # [batch, 3, height*width] = [batch, 3, height*width] * [batch, 3, height*width]
        cam_coords *= depth
        # num_pts = height * width
        num_pts = cam_coords.get_shape().as_list()[2]
        # make homogeneous coordinates
        cam_coords = tf.concat([cam_coords, tf.ones((self.batch, 1, num_pts), tf.float32)], axis=1)
        return cam_coords

    @shape_check
    def transform_to_source(self, tgt_coords, t2s_pose):
        """
        :param tgt_coords: target frame coordinates like (x,y,z,1) [batch, 4, height*width]
        :param t2s_pose: pose matrices that transform points from target to source frame [batch, num_src, 4, 4]
        :return: transformed points in source frame like (x,y,z,1) [batch, num_src, 4, height*width]
        """
        tgt_coords_expand = tf.expand_dims(tgt_coords, 1)
        tgt_coords_expand = tf.tile(tgt_coords_expand, (1, self.num_src, 1, 1))
        # [batch, num_src, 4, height*width] = [batch, num_src, 4, 4] x [batch, num_src, 4, height*width]
        src_coords = tf.matmul(t2s_pose, tgt_coords_expand)
        return src_coords

    def cam2pixel(self, cam_coords, intrinsic):
        """
        :param cam_coords: 3D points in source frame (x,y,z,1) [batch, num_src, 4, height*width]
        :param intrinsic: intrinsic camera matrix [batch, 3, 3]
        :return: projected pixel coordinates on source image plane (u,v,1) [batch, num_src, 3, height*width]
        """
        intrinsic_expand = tf.expand_dims(intrinsic, 1)
        # [batch, num_src, 3, 3]
        intrinsic_expand = tf.tile(intrinsic_expand, (1, self.num_src, 1, 1))

        # [batch, num_src, 3, height*width] = [batch, num_src, 3, 3] x [batch, num_src, 3, height*width]
        point_coords = tf.slice(cam_coords, (0, 0, 0, 0), (-1, -1, 3, -1))
        pixel_coords = tf.matmul(intrinsic_expand, point_coords)
        # pixel_coords = tf.reshape(pixel_coords, (batch, num_src, 3, length))
        # normalize scale
        pixel_scales = pixel_coords[:, :, 2:3, :]
        pixel_coords = pixel_coords / (pixel_scales + 1e-10)
        return pixel_coords
