"""Download the pinned local checkpoint; reuse completed files on subsequent runs."""
import argparse
from huggingface_hub import snapshot_download

parser = argparse.ArgumentParser()
parser.add_argument('--larger', action='store_true')
args = parser.parse_args()
model = 'mlx-community/gemma-3-12b-it-4bit' if args.larger else 'mlx-community/gemma-3-4b-it-4bit'
revision = '86cc6a8dedbc456dd0e4af01a9d09f396f77e558' if args.larger else '93724907d4ed1745d2fe50baadf3b0b01a65abf2'
root = 'build/activation_pilot_12b' if args.larger else 'build/activation_pilot'
print(snapshot_download(model, revision=revision, local_dir=f'{root}/model',
                        allow_patterns=['*.json', '*.safetensors', '*.model'], max_workers=3))
