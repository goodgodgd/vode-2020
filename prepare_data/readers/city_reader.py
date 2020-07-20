import os
import os.path as op
from glob import glob
import json
import numpy as np
import cv2

from utils.util_class import WrongInputException
from prepare_data.readers.reader_base import DataReaderBase


class CityScapesReader(DataReaderBase):
    def __init__(self, base_path, stereo=False, split="", dir_suffix=""):
        super().__init__(base_path, stereo)
        self.pose_avail = False
        self.depth_avail = True
        self.left_img_dir = "leftImg8bit"
        self.split = split
        self.dir_suffix = dir_suffix
        message = "\n!!! ERROR : cityscapes dataset does NOT include calibration results for " \
                  "the right images,\n    so set 'opt.STEREO = False' in config.py.\n"
        assert self.stereo is False, message

    """
    Public methods used outside this class
    """
    def list_drive_paths(self):
        split_path = op.join(self.base_path, self.left_img_dir + self.dir_suffix, self.split)
        if not op.isdir(split_path):
            raise WrongInputException("[list_sequence_paths] path does NOT exist:" + split_path)

        city_names = os.listdir(split_path)
        city_names = [city for city in city_names if op.isdir(op.join(split_path, city))]
        total_sequences = []
        for city in city_names:
            pattern = op.join(split_path, city, "*.png")
            files = glob(pattern)
            seq_numbers = [file.split("_")[-3] for file in files]
            seq_numbers = list(set(seq_numbers))
            seq_numbers.sort()
            seq_paths = [op.join(self.split, city, f"{city}_{seq}") for seq in seq_numbers]
            total_sequences.extend(seq_paths)

        # total_sequences: list of ["city/city_seqind"]
        print("[list_drive_paths]", total_sequences)
        return total_sequences

    def init_drive(self, drive_path):
        """
        reset variables for a new sequence like intrinsic, extrinsic, and last index
        :param drive_path: sequence path like "train/bochum/bochum_000000"
        :return: number of frames

        self.frame_names: full path of frame files without extension
        """
        self.frame_names, self.frame_indices, self.total_num_frames = self._list_frames(drive_path)
        self.intrinsic = self._find_camera_matrix()
        return len(self.frame_names)

    # 여기까지 했고 이제 이 아래를 채우면 돼

    def make_saving_paths(self, dstpath, drive_path):
        """
        :param dstpath: path like "opts.DATAPATH/srcdata/cityscapes_train"
        :param drive_path: sequence path like "train/bochum/bochum_000000"
        :return: [image_path, pose_path, depth_path]
                 specific paths under "dstpath" to save image, pose, and depth
        """
        # e.g. image_path = "opts.DATAPATH/srcdata/cityscapes_train/bochum/bochum_000000"
        image_path = op.join(dstpath, op.basename(drive_path))
        pose_path = op.join(image_path, "pose") if self.pose_avail else None
        depth_path = op.join(image_path, "depth") if self.depth_avail else None
        return image_path, pose_path, depth_path

    def get_image(self, index):
        frame_name = self.frame_names[index]
        image_name = self._make_file_path(self.left_img_dir, frame_name, "png")
        image = cv2.imread(image_name)
        assert image.shape[0] == 1024
        # crop image to remove car body in image
        return image[:768]

    def get_quat_pose(self, index):
        return None

    def get_depth_map(self, index, raw_img_shape=None, target_shape=None):
        frame_name = self.frame_names[index]
        filename = self._make_file_path("disparity", frame_name, "png")
        depth = self._read_depth_map(filename, target_shape)
        return depth

    def get_intrinsic(self):
        return self.intrinsic

    def get_stereo_extrinsic(self):
        return self.T_left_right

    def get_filename(self, example_index):
        filename = op.basename(self.frame_names[example_index])
        return filename.split("_")[-1]

    def get_frame_index(self, example_index):
        return self.frame_indices[example_index]

    """
    Private methods used inside this class
    """
    def _list_frames(self, drive_path):
        """
        :param drive_path: sequence path like "train/bochum/bochum_000000"
        :return frame_files: list of frame paths like ["train/bochum/bochum_000000_000001"]
        """
        frame_pattern = self._make_file_path(self.left_img_dir, drive_path + "_*", "png")
        frame_files = glob(frame_pattern)
        split_city = op.dirname(drive_path)
        # list of ["city_seqind_frameid"]
        frame_files = ["_".join(op.basename(file).split("_")[:-1]) for file in frame_files]
        # list of ["split/city/city_seqind_frameid"]
        frame_files = [op.join(split_city, file) for file in frame_files]
        frame_files.sort()
        frame_indices = [int(file.split("_")[-1]) for file in frame_files]
        total_num_frames = len(frame_files)
        return frame_files, frame_indices, total_num_frames

    def _find_camera_matrix(self):
        drive_path = "_".join(self.frame_names[0].split("_")[:-1])
        # e.g. file_pattern = /path/to/cityscapes/camera/train/bochum/bochum_000000_*_camera.png
        file_pattern = self._make_file_path("camera", drive_path + "_*", "json", False)
        json_files = glob(file_pattern)
        assert json_files, "[_find_camera_matrix] There is no json file in pattern: " + file_pattern
        with open(json_files[0], "r") as fr:
            camera_params = json.load(fr)
        intrinsic = camera_params["intrinsic"]
        fx, fy = intrinsic["fx"], intrinsic["fy"]
        cx, cy = intrinsic["u0"], intrinsic["v0"]
        K = np.array([[fx, 0., cx],
                      [0., fy, cy],
                      [0., 0., 1.]])
        return K

    def _read_depth_map(self, filename, target_shape):
        image = cv2.imread(filename, cv2.IMREAD_ANYDEPTH)
        assert image is not None, f"[_read_depth_map] There is no disparity image " \
                                  + op.basename(filename) + " in split " + self.split
        disparity = (image.astype(np.float32) - 1.) / 256.
        depth = np.where(image > 0., disparity, 0)
        depth = cv2.resize(depth, (target_shape[1], target_shape[0]), cv2.INTER_NEAREST)
        return depth

    def _make_file_path(self, data_dir, frame_name, extension, add_suffix=True):
        if add_suffix:
            dir_name = data_dir + self.dir_suffix
        else:
            dir_name = data_dir
        return op.join(self.base_path, dir_name, frame_name + f"_{data_dir}.{extension}")
