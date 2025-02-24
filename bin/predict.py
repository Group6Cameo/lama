#!/usr/bin/env python3

# Example command:
# ./bin/predict.py \
#       model.path=<path to checkpoint, prepared by make_checkpoint.py> \
#       indir=<path to input data> \
#       outdir=<where to store predicts>

from saicinpainting.utils import register_debug_signal_handlers
from saicinpainting.training.trainers import load_checkpoint
from saicinpainting.training.data.datasets import make_default_val_dataset
from torch.utils.data._utils.collate import default_collate
from omegaconf import OmegaConf
import yaml
import tqdm
import torch
import numpy as np
import hydra
import cv2
import logging
import os
import sys
import traceback

from saicinpainting.evaluation.utils import move_to_device
from saicinpainting.evaluation.refinement import refine_predict
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['VECLIB_MAXIMUM_THREADS'] = '1'
os.environ['NUMEXPR_NUM_THREADS'] = '1'


LOGGER = logging.getLogger(__name__)


@hydra.main(config_path='../configs/prediction', config_name='default.yaml')
def main(predict_config: OmegaConf):
    return process_predict(predict_config)


def process_predict(predict_config: OmegaConf, preloaded_model=None, preloaded_device=None):
    try:
        if sys.platform != 'win32':
            # kill -10 <pid> will result in traceback dumped into log
            register_debug_signal_handlers()

        if preloaded_model is not None and preloaded_device is not None:
            model = preloaded_model
            device = preloaded_device
            device_name = device.type  # Get device name from the device object
            print("Using preloaded model and device")
        else:
            print("Loading model and configuration")
            # Only load model and configuration if not preloaded
            device_name = "cuda" if torch.cuda.is_available() else "cpu"
            device = torch.device(device_name)

            train_config_path = os.path.join(
                predict_config.model.path, 'config.yaml')
            with open(train_config_path, 'r') as f:
                train_config = OmegaConf.create(yaml.safe_load(f))

            train_config.training_model.predict_only = True
            train_config.visualizer.kind = 'noop'

            checkpoint_path = os.path.join(
                predict_config.model.path, 'models', predict_config.model.checkpoint)
            model = load_checkpoint(
                train_config, checkpoint_path, strict=False, map_location=device_name)
            model.freeze()
            model.to(device)

        # Add check for number of available GPUs
        if device_name == "cuda":
            num_gpus = torch.cuda.device_count()
            if num_gpus == 0:
                device_name = "cpu"
                device = torch.device(device_name)
            LOGGER.info(f"Found {num_gpus} GPU(s)")

        out_ext = predict_config.get('out_ext', '.png')

        if not predict_config.get('refine', False):
            pass  # Model is already on the correct device
        else:
            # Pass device information through existing configuration structure
            if 'device_ids' in predict_config.refiner:
                predict_config.refiner.device_ids = [
                    0] if device.type == "cuda" else None

        if not predict_config.indir.endswith('/'):
            predict_config.indir += '/'

        dataset = make_default_val_dataset(
            predict_config.indir, **predict_config.dataset)
        for img_i in tqdm.trange(len(dataset)):
            mask_fname = dataset.mask_filenames[img_i]
            cur_out_fname = os.path.join(
                predict_config.outdir,
                os.path.splitext(mask_fname[len(predict_config.indir):])[
                    0] + out_ext
            )
            os.makedirs(os.path.dirname(cur_out_fname), exist_ok=True)
            batch = default_collate([dataset[img_i]])
            if predict_config.get('refine', False):
                assert 'unpad_to_size' in batch, "Unpadded size is required for the refinement"
                # image unpadding is taken care of in the refiner, so that output image
                # is same size as the input image
                batch = move_to_device(batch, device)
                cur_res = refine_predict(
                    batch, model, **predict_config.refiner)
                cur_res = cur_res[0].permute(1, 2, 0).detach().cpu().numpy()
            else:
                with torch.no_grad():
                    batch = move_to_device(batch, device)
                    batch['mask'] = (batch['mask'] > 0) * 1
                    batch = model(batch)
                    cur_res = batch[predict_config.out_key][0].permute(
                        1, 2, 0).detach().cpu().numpy()
                    unpad_to_size = batch.get('unpad_to_size', None)
                    if unpad_to_size is not None:
                        orig_height, orig_width = unpad_to_size
                        cur_res = cur_res[:orig_height, :orig_width]

            cur_res = np.clip(cur_res * 255, 0, 255).astype('uint8')
            cur_res = cv2.cvtColor(cur_res, cv2.COLOR_RGB2BGR)
            cv2.imwrite(cur_out_fname, cur_res)

    except KeyboardInterrupt:
        LOGGER.warning('Interrupted by user')
    except Exception as ex:
        LOGGER.critical(
            f'Prediction failed due to {ex}:\n{traceback.format_exc()}')
        sys.exit(1)


if __name__ == '__main__':
    main()
