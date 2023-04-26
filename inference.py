import argparse
import json
import os
import os.path as osp
from pathlib import Path
from time import time

from PIL import Image
import cv2
import numpy as np
import torch
from torch import Tensor
from torchvision.transforms import transforms

from models.model import ViTPose
from utils.top_down_eval import keypoints_from_heatmaps
from utils.visualization import draw_points_and_skeleton, joints_dict

__all__ = ['inference']


def pad_image(image: np.ndarray, aspect_ratio: float) -> np.ndarray:
    # Get the current aspect ratio of the image
    image_height, image_width = image.shape[:2]
    current_aspect_ratio = image_width / image_height

    # Determine whether to pad horizontally or vertically
    if current_aspect_ratio < aspect_ratio:
        # Pad horizontally
        target_width = int(aspect_ratio * image_height)
        pad_width = target_width - image_width
        left_pad = pad_width // 2
        right_pad = pad_width - left_pad
        
        padded_image = np.pad(image, pad_width=((0, 0), (left_pad, right_pad), (0, 0)), mode='constant')
    else:
        # Pad vertically
        target_height = int(image_width / aspect_ratio)
        pad_height = target_height - image_height
        top_pad = pad_height // 2
        bottom_pad = pad_height - top_pad
        
        padded_image = np.pad(image, pad_width=((top_pad, bottom_pad), (0, 0), (0, 0)), mode='constant')
    
    return padded_image


class VideoReader(object):
    def __init__(self, file_name):
        self.file_name = file_name
        try:  # OpenCV needs int to read from webcam
            self.file_name = int(file_name)
        except ValueError:
            pass

    def __iter__(self):
        self.cap = cv2.VideoCapture(self.file_name)
        if not self.cap.isOpened():
            raise IOError('Video {} cannot be opened'.format(self.file_name))
        return self

    def __next__(self):
        was_read, img = self.cap.read()
        if not was_read:
            raise StopIteration
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


@torch.no_grad()
def inference(img: np.ndarray, target_size: tuple[int, int], model,
              device: torch.device) -> np.ndarray:

    # Prepare input data
    org_h, org_w = img.shape[:2]
    img_tensor = transforms.Compose([transforms.ToTensor(),
                                     transforms.Resize((target_size[1], target_size[0])),
                                     # transforms.Normalize(mean=[0.485, 0.456, # TODO ?
                                     #                            0.406],
                                     #                      std=[0.229, 0.224,
                                     #                           0.225]),
                                     ])(img).unsqueeze(0).to(device)

    # Feed to model
    heatmaps = model(img_tensor).detach().cpu().numpy()
    points, prob = keypoints_from_heatmaps(heatmaps=heatmaps,
                                           center=np.array([[org_w // 2,
                                                             org_h // 2]]),
                                           scale=np.array([[org_w, org_h]]),
                                           unbiased=True, use_udp=True)

    points = np.concatenate([points[:, :, ::-1], prob], axis=2)
    return points


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=str, default='examples/sample.jpg',
                        help='image or video path')
    parser.add_argument('--output-path', type=str, default='', help='output path')
    parser.add_argument('--model', type=str, required=True, help='ckpt path')
    parser.add_argument('--model-name', type=str, required=True,
                        help='[b: ViT-B, l: ViT-L, h: ViT-H]')
    parser.add_argument('--show', default=False, action='store_true',
                        help='preview result')
    parser.add_argument('--save-img', default=False, action='store_true',
                        help='save image result')
    parser.add_argument('--save-json', default=False, action='store_true',
                        help='save json result')
    args = parser.parse_args()

    if args.model_name == 'b':
        from configs.ViTPose_base_coco_256x192 import model as model_cfg
        from configs.ViTPose_base_coco_256x192 import data_cfg
    elif args.model_name == 'l':
        from configs.ViTPose_large_coco_256x192 import model as model_cfg
        from configs.ViTPose_large_coco_256x192 import data_cfg
    elif args.model_name == 'h':
        from configs.ViTPose_huge_coco_256x192 import model as model_cfg
        from configs.ViTPose_huge_coco_256x192 import data_cfg

    input_path = args.input
    ext = input_path[input_path.rfind('.'):]
    img_size = data_cfg['image_size']

    # Load the model
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device('cpu')
    vit_pose = ViTPose(model_cfg)

    ckpt = torch.load(args.model, map_location='cpu')
    if 'state_dict' in ckpt:
        vit_pose.load_state_dict(ckpt['state_dict'])
    else:
        vit_pose.load_state_dict(ckpt)
    vit_pose.to(device)
    print(f">>> Model loaded: {args.model} on {device}")

    # Load the image / video reader
    assert os.path.isfile(input_path), 'The input file does not exist'
    if input_path[input_path.rfind('.'):] in ['mp4']:
        reader = VideoReader(input_path)
    else:
        reader = [np.array(Image.open(input_path))]

    print(f'Running inference on {input_path}')
    keypoints = []
    for img in reader:
        img = pad_image(img, 3 / 4)
        k = inference(img=img, target_size=img_size, model=vit_pose,
                      device=torch.device("cuda") if torch.cuda.is_available()
                      else torch.device('cpu'))
        keypoints.append(k)

        if args.show or args.save_img:
            for pid, point in enumerate(k):
                img = np.array(img)[:, :, ::-1]  # RGB to BGR for cv2 modules
                img = draw_points_and_skeleton(img.copy(), point,
                                               joints_dict()['coco']['skeleton'],
                                               person_index=pid,
                                               points_color_palette='gist_rainbow',
                                               skeleton_color_palette='jet',
                                               points_palette_samples=10,
                                               confidence_threshold=0.4)

            if args.save_img:
                save_name = os.path.basename(input_path).replace(ext, f"_result{ext}")
                cv2.imwrite(os.path.join(args.output_path, save_name), img)

            if args.show:
                cv2.imshow('preview', img)
                cv2.waitKey(0)

    if args.save_json:
        print('>>> Saving output json')
        save_name = os.path.basename(input_path).replace(ext, f"_result.json")
        with open(os.path.join(args.output_path, save_name), 'w') as f:
            out = [{p: v.tolist() for p, v in enumerate(k)} for k in keypoints]
            json.dump(out, f)

    cv2.destroyAllWindows()
