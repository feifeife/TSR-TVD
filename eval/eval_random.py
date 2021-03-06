# model evaluation

import os
import argparse
import math
import pdb

import numpy as np
from tqdm import tqdm
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import Dataset, DataLoader
from torchvision.utils import save_image
from torchvision import transforms

import sys
sys.path.append("../")
sys.path.append("../datasets")
sys.path.append("../model")
from generator import Generator
from discriminator import Discriminator
from trainDataset import *
from utils import *

def parse_args():
    parser = argparse.ArgumentParser(description="Deep Learning Model")

    parser.add_argument("--no-cuda", action="store_true" , default=False,
                        help="disable CUDA training")
    parser.add_argument("--data-parallel", action="store_true", default=False,
                        help="enable data parallelism")
    parser.add_argument("--seed", type=int, default=1,
                        help="random seed (default: 1)")

    parser.add_argument("--root", required=True, type=str,
                        help="root of the dataset")
    parser.add_argument("--save-pred", required=True, type=str,
                        help="dir of predicted volumes")
    parser.add_argument("--resume", type=str, default="",
                        help="path to the latest checkpoint (default: none)")
    parser.add_argument("--volume-train-list", type=str, default="volume_train_list.txt")
    parser.add_argument("--volume-test-list", type=str, default="volume_test_list.txt")

    parser.add_argument("--gen-sn", action="store_true", default=False,
                        help="enable spectral normalization for the generator")

    parser.add_argument("--gan-loss", type=str, default="none",
                        help="gan loss (default: none)")
    parser.add_argument("--volume-loss", action="store_true", default=False,
                        help ="enable volume loss")
    parser.add_argument("--feature-loss", action="store_true", default=False,
                        help="enable feature loss")
    parser.add_argument("--gan-loss-weight" , type=float, default=1e-3,
                        help="weight of the adversarial loss")
    parser.add_argument("--volume-loss-weight", type=float, default=1,
                        help="weight of the volume loss (mse)")
    parser.add_argument("--feature-loss-weight", type=float, default=5e-2,
                        help="weight of the feature loss")
    parser.add_argument("--wo-ori-volume", action="store_true", default=False,
                        help="during training, without the original volume")
    parser.add_argument("--upsample-mode", type=str, default="lr",
                        help="how to do upsample, voxel shuffle (lr) or interpolate (hr)")
    parser.add_argument("--norm", type=str, default="",
                        help="how normalize hidden layer, none or batch norm or instance norm")
    parser.add_argument("--forward", action="store_true", default=False,
                        help="during training, do forward prediction")
    parser.add_argument("--backward", action="store_true", default=False,
                        help="during training, do backward prediction")

    parser.add_argument("--lr", type=float, default=1e-4,
                        help="learning rate (default: 1e-4)")
    parser.add_argument("--d-lr", type=float, default=4e-4,
                        help="learning rate of the discriminator (default 4e-4)")
    parser.add_argument("--beta1", type=float, default=0.0,
                        help="beta1 of Adam (default: 0.0)")
    parser.add_argument("--beta2", type=float, default=0.999,
                        help="beta2 of Adam (default: 0.999)")
    parser.add_argument("--batch-size", type=int, default=1,
                        help="batch size for training (default: 4)")
    parser.add_argument("--training-step", type=int, default=3,
                        help="in the training phase, the number of intermediate volumes")
    parser.add_argument("--n-d", type=int, default=2,
                        help="number of D updates per iteration")
    parser.add_argument("--n-g", type=int, default=1,
                        help="number of G upadates per iteration")
    parser.add_argument("--start-epoch", type=int, default=0,
                        help="start epoch number (default: 0)")
    parser.add_argument("--epochs", type=int, default=100,
                        help="number of epochs to train (default: 10)")

    parser.add_argument("--log-every", type=int, default=3,
                        help="log training status every given number of batches (default: 10)")
    parser.add_argument("--test-every", type=int, default=9,
                        help="test every given number of epochs (default: 5")
    parser.add_argument("--check-every", type=int, default=30,
                        help="save checkpoint every given number of epochs (default: 20)")

    parser.add_argument("--block-size", type=int, default=64,
                        help="the size of the sub-block")
    return parser.parse_args()

# the main function
def main(args):
    # log hyperparameter
    print(args)

    # select device
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device("cuda: 0" if args.cuda else "cpu")

    # set random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # data loader
    transform = transforms.Compose([
        Normalize(),
        ToTensor()
    ])
    train_dataset = TVDataset(
        root=args.root,
        sub_size=args.block_size,
        volume_list=args.volume_train_list,
        max_k=args.training_step,
        train=True,
        transform=transform
    )
    test_dataset = TVDataset(
        root=args.root,
        sub_size=args.block_size,
        volume_list=args.volume_test_list,
        max_k=args.training_step,
        train=False,
        transform=transform
    )

    kwargs = {"num_workers": 4, "pin_memory": True} if args.cuda else {}
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size,
                              shuffle=False, **kwargs)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size,
                             shuffle=False, **kwargs)

    # model
    def generator_weights_init(m):
        if isinstance(m, nn.Conv3d):
            nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            if m.bias is not None:
                nn.init.zeros_(m.bias)

    g_model = Generator(args.upsample_mode, args.forward, args.backward, args.gen_sn)
    g_model.apply(generator_weights_init)
    if args.data_parallel and torch.cuda.device_count() > 1:
        g_model = nn.DataParallel(g_model)
    g_model.to(device)

    mse_loss = nn.MSELoss()
    adversarial_loss = nn.MSELoss()
    train_losses, test_losses = [], []
    d_losses, g_losses = [], []

    # load checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint {}".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint["epoch"]
            g_model.load_state_dict(checkpoint["g_model_state_dict"])
            # g_optimizer.load_state_dict(checkpoint["g_optimizer_state_dict"])
            if args.gan_loss != "none":
                # d_model.load_state_dict(checkpoint["d_model_state_dict"])
                # d_optimizer.load_state_dict(checkpoint["d_optimizer_state_dict"])
                d_losses = checkpoint["d_losses"]
                g_losses = checkpoint["g_losses"]
            train_losses = checkpoint["train_losses"]
            test_losses = checkpoint["test_losses"]
            print("=> load chekcpoint {} (epoch {})"
                  .format(args.resume, checkpoint["epoch"]))

    # evaluating 
    # 1) plot losses 
    # pdb.set_trace()
    # fig, ax = plt.subplots()
    # ax.set(xlabel=u"SubEpoches", ylabel=u"loss")
    # ax.set_ylim(0., 0.02)
    # plt.plot(g_losses, label="g_loss")
    # plt.legend()
    # plt.show()


    # 3) compute PSNR
    g_model.eval()
    test_loss = 0.
    test_loss_tsr = 0.
    test_loss_lerp = 0.
    with torch.no_grad():
        for i, sample in enumerate(test_loader):
            # print(sample["idx"].item(), sample["vf_name"])
            v_f = sample["v_f"].to(device)
            v_b = sample["v_b"].to(device)
            v_i = sample["v_i"].to(device)
            fake_volumes = g_model(v_f, v_b, args.training_step, args.wo_ori_volume, args.norm)
            test_loss += args.volume_loss_weight * mse_loss(v_i, fake_volumes).item()

            for j in range(fake_volumes.shape[1]):
                min_value = -0.015  # -0.012058
                max_value = 1.01  # 1.009666
                mean = (min_value + max_value) / 2
                std = mean - min_value

                real = v_i[0, j, 0]
                # real = real.to("cpu").numpy() * std + mean
                real = real.to("cpu").numpy()
                diff = real.max() - real.min()

                tsr = fake_volumes[0, j, 0]
                # tsr = tsr.to("cpu").numpy() * std + mean
                tsr = tsr.to("cpu").numpy()
                mse_tsr = np.mean(np.power(tsr - real, 2.))
                # psnr_tsr = 20. * np.log10(diff) - 10. * np.log10(mse_tsr)

                offset = j + 1
                interval = args.training_step + 1
                lerp = (1-offset/interval) * v_f + offset/interval * v_b
                # lerp = lerp.to("cpu").numpy() * std + mean
                lerp = lerp.to("cpu").numpy()
                mse_lerp = np.mean(np.power(lerp - real, 2.))
                # psnr_lerp = 20. * np.log10(diff) - 10. * np.log10(mse_lerp)

                # pdb.set_trace()
                print("sample {}, intermediate {}, TSR MSE {:.6f}, LERP MSE {:.6f}"
                      .format(sample["vf_name"], j+1, mse_tsr, mse_lerp))
                test_loss_tsr += mse_tsr / fake_volumes.shape[1]
                test_loss_lerp += mse_lerp / fake_volumes.shape[1]
                # volume.tofile(os.path.join(args.save_pred, sample["vi_name"][j][0]))
            # if (args.volume_loss_weight * mse_loss(v_i, fake_volumes).item() > 0.004):
            #     print("{} {}".format(sample["vf_name"], args.volume_loss_weight * mse_loss(v_i, fake_volumes).item()))

    #     pdb.set_trace()
        print("====> Test set loss TSR {:4f} LERP {:4f}".format(
            test_loss_tsr / len(test_loader.dataset), test_loss_lerp / len(test_loader.dataset)
        ))
        
if __name__ == "__main__":
    main(parse_args())

