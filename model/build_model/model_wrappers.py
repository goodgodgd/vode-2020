import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
import os.path as op

from config import opts
import utils.util_funcs as uf


class ModelWrapper:
    """
    tf.keras.Model output formats according to prediction methods
    1) preds = model(image_tensor) -> dict('disp_ms': disp_ms, 'pose': pose)
        disp_ms: list of [batch, height/scale, width/scale, 1]
        pose: [batch, numsrc, 6]
    2) preds = model.predict(image_tensor) -> [disp_s1, disp_s2, disp_s4, disp_s8, pose]
    3) preds = model.predict({'image':, ...}) -> [disp_s1, disp_s2, disp_s4, disp_s8, pose]
    """
    def __init__(self, models, augmenter):
        self.models = models
        self.augmenter = augmenter

    def __call__(self, features):
        features = self.augmenter(features)
        predictions = self.predict_batch(features)
        return predictions

    def predict(self, dataset, total_steps):
        print(f"===== [ModelWrapper] start prediction")
        outputs = {name[:-3]: [] for name, model in self.models.items()}
        for step, features in enumerate(dataset):
            predictions = self.predict_batch(features)
            outputs = self.append_outputs(predictions, outputs)
            uf.print_progress_status(f"Progress: {step} / {total_steps}")

        print("")
        # concatenate batch outputs along batch axis
        for key, data in outputs.items():
            outputs[key] = np.concatenate(data, axis=0)
        return outputs

    def predict_batch(self, features, suffix=""):
        predictions = dict()
        for netname, model in self.models.items():
            pred = model(features["image_aug" + suffix])
            predictions.update(pred)

        predictions = {key + suffix: value for key, value in predictions.items()}
        return predictions

    def append_outputs(self, predictions, outputs, suffix=""):
        if "pose" + suffix in predictions:
            pose = predictions["pose" + suffix]         # [batch, numsrc, 6]
            outputs["pose" + suffix].append(pose)
        # only the highest resolution ouput is used for evaluation
        if "depthnet" + suffix in self.models:
            depth_ms = predictions["depth_ms" + suffix] # [batch, height, width, 1]
            outputs["depth" + suffix].append(depth_ms[0])
        if "flownet" + suffix in self.models:
            flow_ms = predictions["flow_ms" + suffix]   # [batch, numsrc, height, width, 2]
            outputs["flow" + suffix].append(flow_ms[0])
        return outputs

    def compile(self, optimizer="sgd", loss="mean_absolute_error"):
        for model in self.models.values():
            model.compile(optimizer=optimizer, loss=loss)

    def trainable_weights(self):
        train_weights = []
        for model in self.models.values():
            train_weights.extend(model.trainable_weights)
        return train_weights

    def weights_to_regularize(self):
        if "flownet" in self.models:
            return self.models["flownet"].trainable_weights
        else:
            return None

    def save_weights(self, ckpt_dir_path, suffix):
        for netname, model in self.models.items():
            save_path = op.join(ckpt_dir_path, f"{netname}_{suffix}.h5")
            model.save_weights(save_path)

    def load_weights(self, ckpt_dir_path, suffix):
        for netname in self.models.keys():
            ckpt_file = op.join(ckpt_dir_path, f"{netname}_{suffix}.h5")
            if op.isfile(ckpt_file):
                self.models[netname].load_weights(ckpt_file)
                print(f"===== {netname} weights loaded from", ckpt_file)
            else:
                print(f"===== Failed to load weights of {netname}, train from scratch ...")
                print(f"      tried to load file:", ckpt_file)

    def summary(self, **kwargs):
        for model in self.models.values():
            model.summary(**kwargs)

    def inputs(self):
        return [model.input for model in self.models.values()]

    def outputs(self):
        output_dict = dict()
        for model in self.models.values():
            output_dict.update(model.output)
        return output_dict

    def plot_model(self, dir_path):
        for netname, model in self.models.items():
            tf.keras.utils.plot_model(model, to_file=op.join(dir_path, netname + ".png"), show_shapes=True)


class StereoModelWrapper(ModelWrapper):
    def __init__(self, models, augmenter):
        super().__init__(models, augmenter)

    def __call__(self, features):
        features = self.augmenter(features)
        predictions = self.predict_batch(features)
        preds_right = self.predict_batch(features, "_R")
        predictions.update(preds_right)
        return predictions

    def predict(self, dataset, total_steps):
        print(f"===== [ModelWrapper] start prediction")
        outputs = {name[:-3]: [] for name, model in self.models.items()}
        outputs_right = {name[:-3] + "_R": [] for name, model in self.models.items()}
        outputs.update(outputs_right)

        for step, features in enumerate(dataset):
            predictions = self.predict_batch(features)
            preds_right = self.predict_batch(features, "_R")
            predictions.update(preds_right)
            outputs = self.append_outputs(predictions, outputs)
            outputs = self.append_outputs(predictions, outputs, "_R")
            uf.print_progress_status(f"Progress: {step} / {total_steps}")

        print("")
        # concatenate batch outputs along batch axis
        for key, data in outputs.items():
            outputs[key] = tf.concat(data, axis=0)
        return outputs


class StereoPoseModelWrapper(ModelWrapper):
    def __init__(self, models, augmenter):
        super().__init__(models, augmenter)

    def __call__(self, features):
        features = self.augmenter(features)
        predictions = self.predict_batch(features)
        preds_right = self.predict_batch(features, "_R")
        predictions.update(preds_right)
        if "posenet" in self.models:
            stereo_pose = self.predict_stereo_pose(features)
            predictions.update(stereo_pose)
        return predictions

    def predict_stereo_pose(self, features):
        # predicts stereo extrinsic in both directions: left to right, right to left
        posenet = self.models["posenet"]
        left_target = features["image_aug"][:, -1]
        right_target = features["image_aug_R"][:, -1]
        numsrc = opts.SNIPPET_LEN - 1
        lr_input = tf.stack([right_target] * numsrc + [left_target], axis=1)
        rl_input = tf.stack([left_target] * numsrc + [right_target], axis=1)
        # lr_input = layers.concatenate([right_target] * numsrc + [left_target], axis=1)
        # rl_input = layers.concatenate([left_target] * numsrc + [right_target], axis=1)

        # pose that transforms points from right to left (T_LR)
        pose_lr = posenet(lr_input)
        # pose that transforms points from left to right (T_RL)
        pose_rl = posenet(rl_input)
        outputs = {"pose_LR": pose_lr["pose"], "pose_RL": pose_rl["pose"]}
        return outputs
