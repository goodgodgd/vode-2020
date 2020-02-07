import os.path as op


class VodeOptions:
    """
    data options
    """
    SNIPPET_LEN = 5
    IM_WIDTH = 384
    IM_HEIGHT = 128
    MIN_DEPTH = 1e-3
    MAX_DEPTH = 80

    """
    training options
    """
    BATCH_SIZE = 8
    EPOCHS = 100
    LEARNING_RATE = 0.0002
    ENABLE_SHAPE_DECOR = False
    CKPT_NAME = "vode1"

    """
    path options
    """
    DATAPATH = "/media/ian/IanPrivatePP/Datasets/vode_data_384"
    assert(op.isdir(DATAPATH))
    DATAPATH_SRC = op.join(DATAPATH, "srcdata")
    DATAPATH_TFR = op.join(DATAPATH, "tfrecords")
    DATAPATH_CKP = op.join(DATAPATH, "checkpts")
    DATAPATH_LOG = op.join(DATAPATH, "log")
    DATAPATH_PRD = op.join(DATAPATH, "prediction")
    DATAPATH_EVL = op.join(DATAPATH, "evaluation")

    """
    model options: network architecture, loss wegihts, ...
    """
    PHOTO_LOSS = "L1"
    SMOOTH_WEIGHT = 0.5
    DATASET = "kitti_raw"
    NET_NAMES = {"depth": "NASNetMobile", "camera": "pose_only"}
    SYNTHESIZER = "synthesize_multi_scale"
    OPTIMIZER = "adam_constant"
    PRETRAINED_WEIGHT = True


opts = VodeOptions()


# TODO: add or change RAW_DATA_PATHS as dataset paths in your PC
RAW_DATA_PATHS = {
    "kitti_raw": "/media/ian/IanPrivatePP/Datasets/kitti_raw_data",
    "kitti_odom": "/media/ian/IanPrivatePP/Datasets/kitti_odometry",
}


class WrongDatasetException(Exception):
    def __init__(self, msg):
        super().__init__(msg)


def get_raw_data_path(dataset_name):
    if dataset_name in RAW_DATA_PATHS:
        dataset_path = RAW_DATA_PATHS[dataset_name]
        assert op.isdir(dataset_path)
        return dataset_path
    else:
        raise WrongDatasetException(f"Unavailable dataset name, available datasets are {list(RAW_DATA_PATHS.keys())}")
