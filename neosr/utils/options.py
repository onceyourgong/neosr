import argparse
import os
import random
import sys
import tomllib
from os import path as osp
from pathlib import PosixPath
from typing import Any

import torch

from neosr.utils import set_random_seed
from neosr.utils.dist_util import get_dist_info, init_dist, master_only


def toml_load(f) -> dict[str, Any]:
    """Load TOML file
    Args:
        f (str): File path or a python string.

    Returns
    -------
        dict: Loaded dict.

    """
    try:
        with open(f, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        print("Error decoding TOML file.")
        sys.exit(1)


def parse_options(
    root_path: PosixPath | str, is_train: bool = True
) -> tuple[dict[str, Any] | None, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        prog="neosr",
        usage=argparse.SUPPRESS,
        description="""-------- neosr command-line options --------""",
    )

    parser._optionals.title = "training and inference"

    parser.add_argument(
        "-opt", type=str, required=False, help="Path to option TOML file."
    )

    parser.add_argument(
        "--launcher",
        choices=["none", "pytorch", "slurm"],
        default="none",
        help="job launcher",
    )

    parser.add_argument("--auto_resume", action="store_true", default=False)

    parser.add_argument("--debug", action="store_true")

    parser.add_argument("--local_rank", type=int, default=0)

    # Options for convert.py script

    group = parser.add_argument_group("model conversion")

    group.add_argument(
        "--input", type=str, required=False, help="Input Pytorch model path."
    )

    group.add_argument(
        "-onnx",
        "--onnx",
        action="store_true",
        help="Enables ONNX conversion.",
        default=False,
    )

    group.add_argument(
        "-safetensor",
        "--safetensor",
        action="store_true",
        help="Enables safetensor conversion.",
        default=False,
    )

    group.add_argument(
        "-net", "--network", type=str, required=False, help="Generator network."
    )

    group.add_argument("-s", "--scale", type=int, help="Model scale ratio.", default=4)

    group.add_argument(
        "-window", "--window", type=int, help="Model scale ratio.", default=None
    )

    group.add_argument(
        "-opset", "--opset", type=int, help="ONNX opset. (default: 17)", default=17
    )

    group.add_argument(
        "-static",
        "--static",
        type=int,
        nargs=3,
        help='Set static shape for ONNX conversion. Example: -static "3,640,640".',
        default=None,
    )

    group.add_argument(
        "-nocheck",
        "--nocheck",
        action="store_true",
        help="Disables checking against original pytorch model on ONNX conversion.",
        default=False,
    )

    group.add_argument(
        "-fp16",
        "--fp16",
        action="store_true",
        help="Enable half-precision. (default: false)",
        default=False,
    )

    group.add_argument(
        "-optimize",
        "--optimize",
        action="store_true",
        help="Run ONNX optimizations",
        default=False,
    )

    group.add_argument(
        "-fulloptimization",
        "--fulloptimization",
        action="store_true",
        help="Run full ONNX optimizations",
        default=False,
    )

    group.add_argument(
        "--output",
        type=str,
        required=False,
        help="Output ONNX model path.",
        default=root_path,
    )

    args = parser.parse_args()

    # error if no config file exists
    if args.input is None and not osp.exists(args.opt):
        msg = "Didn't get a config! Please link the config file using -opt /path/to/config.toml"
        raise ValueError(msg)

    if args.input is None:
        # error if not toml
        if not args.opt.endswith(".toml"):
            msg = """
            neosr only support TOML configuration files now,
            please see template files on the options/ folder.
            """
            raise ValueError(msg)

        # parse toml to dict
        opt = toml_load(args.opt)

        # distributed settings
        if args.launcher == "none":
            opt["dist"] = False
        else:
            opt["dist"] = True
            if args.launcher == "slurm" and "dist_params" in opt:
                init_dist(args.launcher, **opt["dist_params"])
            else:
                init_dist(args.launcher)
        opt["rank"], opt["world_size"] = get_dist_info()

        # random seed
        seed = opt.get("manual_seed")
        if seed is None:
            opt["deterministic"] = False
            seed = random.randint(1024, 10000)
            opt["manual_seed"] = seed
            torch.backends.cudnn.benchmark = True
            torch.backends.cudnn.benchmark_limit = 0
        else:
            # Determinism
            opt["deterministic"] = True
            os.environ["PYTHONHASHSEED"] = str(seed)
            os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
            torch.use_deterministic_algorithms(True, warn_only=True)
        set_random_seed(seed + opt["rank"])

        opt["auto_resume"] = args.auto_resume
        opt["is_train"] = is_train

        # debug setting
        if args.debug and not opt["name"].startswith("debug"):
            opt["name"] = "debug_" + opt["name"]

        if opt.get("num_gpu", "auto") == "auto":
            opt["num_gpu"] = torch.cuda.device_count()

        # datasets
        for phase, dataset in opt["datasets"].items():
            # for multiple datasets, e.g., val_1, val_2; test_1, test_2
            phase = phase.split("_")[0]
            dataset["phase"] = phase
            if "scale" in opt:
                dataset["scale"] = opt["scale"]
            if dataset.get("dataroot_gt") is not None:
                dataset["dataroot_gt"] = osp.expanduser(dataset["dataroot_gt"])
            if dataset.get("dataroot_lq") is not None:
                dataset["dataroot_lq"] = osp.expanduser(dataset["dataroot_lq"])

        # paths
        if opt.get("path") is not None:
            for key, val in opt["path"].items():
                if (val is not None) and (
                    "resume_state" in key or "pretrain_network" in key
                ):
                    opt["path"][key] = osp.expanduser(val)

        if is_train:
            experiments_root = opt.get("path")
            if experiments_root is not None:
                experiments_root = experiments_root.get("experiments_root")
            if experiments_root is None:
                experiments_root = osp.join(root_path, "experiments")
            experiments_root = osp.join(experiments_root, opt["name"])

            if opt.get("path") is None:
                opt["path"] = {}

            opt["path"]["experiments_root"] = experiments_root
            opt["path"]["models"] = osp.join(experiments_root, "models")
            opt["path"]["training_states"] = osp.join(
                experiments_root, "training_states"
            )
            opt["path"]["log"] = experiments_root
            opt["path"]["visualization"] = osp.join(experiments_root, "visualization")

            # change some options for debug mode
            if "debug" in opt["name"]:
                if "val" in opt:
                    opt["val"]["val_freq"] = 8
                opt["logger"]["print_freq"] = 1
                opt["logger"]["save_checkpoint_freq"] = 8
        else:  # test
            results_root = opt["path"].get("results_root")
            if results_root is None:
                results_root = osp.join(root_path, "experiments", "results")
            results_root = osp.join(results_root, opt["name"])

            opt["path"]["results_root"] = results_root
            opt["path"]["log"] = results_root
            opt["path"]["visualization"] = results_root
    else:
        opt = None

    return opt, args


@master_only
def copy_opt_file(opt_file: str, experiments_root: str):
    # copy the toml file to the experiment root
    import sys
    import time
    from shutil import copyfile

    cmd = " ".join(sys.argv)
    filename = osp.join(experiments_root, osp.basename(opt_file))
    copyfile(opt_file, filename)

    with open(filename, "r+") as f:
        lines = f.readlines()
        lines.insert(0, f"# GENERATE TIME: {time.asctime()}\n# CMD:\n# {cmd}\n\n")
        f.seek(0)
        f.writelines(lines)
