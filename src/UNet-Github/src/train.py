import argparse
import random
import os
import numpy as np
import pickle as pkl
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import metrics
from datasets import SEN12MS, DFC2020
from models.deeplab import DeepLab
from models.unet import UNet
from utils import display_input_batch, display_label_batch


class ModelTrainer:

    def __init__(self, args):
        self.args = args

    # main training function (trains for one epoch)
    def train(self, model, train_loader, val_loader, loss_fn, optimizer,
              writer, step=0):

        # set model to train mode
        model.train()

        # get class scheme
        no_savanna = train_loader.dataset.no_savanna

        # get index of channel(s) suitable for previewing the input images
        display_channels = train_loader.dataset.display_channels
        brightness_factor = train_loader.dataset.brightness_factor

        # main training loop
        pbar = tqdm(total=len(train_loader), desc="[Train]")
        training_loss = 0
        for i, batch in enumerate(train_loader):

            # unpack sample
            image, target = batch['image'], batch['label']

            # reset gradients
            optimizer.zero_grad()

            # move data to gpu if model is on gpu
            if self.args.use_gpu:
                image, target = image.cuda(), target.cuda()

            # forward pass
            prediction = model(image)
            training_loss += loss_fn(prediction, target).data.item()
            loss = loss_fn(prediction, target)

            # backward pass
            loss.backward()
            optimizer.step()

            # log progress, validate, and save checkpoint
            global_step = i + step

            # update progressbar

            pbar.set_description("[Train] Loss: {:.4f}".format(
                                    round(loss.item(), 4)))
            pbar.update()

        # close progressbar and flush to disk
        pbar.close()
        writer.flush()
        return (model, global_step)

    # main validation function (validates current model)
    def val(self, model, dataloader, step, loss_fn, writer):

        # set model to evaluation mode
        model.eval()

        # main validation loop
        pbar = tqdm(total=len(dataloader), desc="[Val]")
        loss = 0
        conf_mat = metrics.ConfMatrix(dataloader.dataset.n_classes)
        for i, batch in enumerate(dataloader):

            # unpack sample
            image, target = batch['image'], batch['label']

            # move data to gpu if model is on gpu
            if self.args.use_gpu:
                image, target = image.cuda(), target.cuda()

            # forward pass
            with torch.no_grad():
                prediction = model(image)
            loss += loss_fn(prediction, target).cpu().item()

            # calculate error metrics
            conf_mat.add_batch(target, prediction.max(1)[1])

            # update progressbar
            pbar.update()

        # close progressbar
        pbar.set_description("[Val] AA: {:.2f}%".format(
            conf_mat.get_aa() * 100))
        pbar.close()

        writer.flush()
        model.train()
        return

    def export_model(self, model, optimizer=None, name=None, step=None):

        # set output filename
        if name is not None:
            out_file = name
        else:
            out_file = "checkpoint"
            if step is not None:
                out_file += "_step_" + str(step)
        out_file = os.path.join(self.args.checkpoint_dir, out_file + ".pth")

        # save model
        data = {"model_state_dict": model.state_dict()}
        if step is not None:
            data["step"] = step
        if optimizer is not None:

            data["optimizer_state_dict"] = optimizer.state_dict()
        torch.save(data, out_file)


def main():

    # define and parse arguments
    parser = argparse.ArgumentParser()

    # general
    parser.add_argument('--experiment_name', type=str, default="experiment",
                        help="experiment name. will be used in the path names \
                             for log- and savefiles")
    parser.add_argument('--seed', type=int, default=42,
                        help='fixes random seed and sets model to \
                              the potentially faster cuDNN deterministic mode \
                              (default: deterministic model with seed 42)')
    parser.add_argument('--val_freq', type=int, default=2,
                        help='validation will be run every val_freq \
                        batches/optimization steps during training')
    parser.add_argument('--save_freq', type=int, default=2,
                        help='training state will be saved every save_freq \
                        batches/optimization steps during training')
    # input/output
    parser.add_argument('--use_s2hr', action='store_true', default=True,
                        help='use sentinel-2 high-resolution (10 m) bands')
    parser.add_argument('--use_s2mr', action='store_true', default=False,
                        help='use sentinel-2 medium-resolution (20 m) bands')
    parser.add_argument('--use_s2lr', action='store_true', default=False,
                        help='use sentinel-2 low-resolution (60 m) bands')
    parser.add_argument('--use_s1', action='store_true', default=False,
                        help='use sentinel-1 data')
    parser.add_argument('--no_savanna', action='store_true', default=True,
                        help='ignore class savanna')

    # training hyperparameters
    parser.add_argument('--lr', type=float, default=0.01,
                        help='learning rate (default: 1e-2)')
    parser.add_argument('--momentum', type=float, default=0.9,
                        help='momentum (default: 0.9), only used for deeplab')
    parser.add_argument('--weight_decay', type=float, default=5e-4,
                        help='weight-decay (default: 5e-4)')
    parser.add_argument('--batch_size', type=int, default=8,
                        help='batch size for training and validation \
                              (default: 32)')
    parser.add_argument('--workers', type=int, default=4,
                        help='number of workers for dataloading (default: 4)')
    parser.add_argument('--max_epochs', type=int, default=1,
                        help='number of training epochs (default: 10)')

    # network
    parser.add_argument('--model', type=str, choices=['deeplab', 'unet'],
                        default='unet',
                        help="network architecture (default: unet)")

    # deeplab-specific
    parser.add_argument('--pretrained_backbone', action='store_true',
                        default=False,
                        help='initialize ResNet-101 backbone with ImageNet \
                              pre-trained weights')
    parser.add_argument('--out_stride', type=int, choices=[8, 16], default=16,
                        help='network output stride (default: 16)')

    # data
    parser.add_argument('--data_dir_train', type=str, default='../../../data/toy_s2',
                        help='path to training dataset')
    parser.add_argument('--dataset_val', type=str, default="val",
                        choices=['sen12ms_holdout', 'dfc2020_val',
                                 'dfc2020_test'],
                        help='dataset to use for validation (default: \
                             sen12ms_holdout)')
    parser.add_argument('--data_dir_val', type=str, default='../../../data/toy_s2',
                        help='path to validation dataset')
    
    args = parser.parse_args()
    print("="*20, "CONFIG", "="*20)
    for arg in vars(args):
        print('{0:20}  {1}'.format(arg, getattr(args, arg)))
    print()

    # fix seeds and set pytorch to deterministic mode
    if args.seed is not None:
        torch.manual_seed(args.seed)
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    # set flags for GPU processing if available
    if torch.cuda.is_available():
        args.use_gpu = True
        if torch.cuda.device_count() > 1:
            raise NotImplementedError("multi-gpu training not implemented! "
                                      + "try to run script as: "
                                      + "CUDA_VISIBLE_DEVICES=0 train.py")
    else:
        args.use_gpu = False

    # load datasets
    '''train_set = SEN12MS(args.data_dir_train,
                        subset="train",
                        no_savanna=args.no_savanna,
                        use_s2hr=args.use_s2hr,
                        use_s2mr=args.use_s2mr,
                        use_s2lr=args.use_s2lr,
                        use_s1=args.use_s1)'''
    
    train_set = DFC2020(args.data_dir_train,
                        subset = "train", 
                        no_savanna=args.no_savanna,
                        use_s2hr=args.use_s2hr,
                        use_s2mr=args.use_s2mr,
                        use_s2lr=args.use_s2lr,
                        use_s1=args.use_s1)
    #select 10 frst images 
    n_classes = train_set.n_classes
    n_inputs = train_set.n_inputs
    if args.dataset_val == "sen12ms_holdout":
        val_set = SEN12MS(args.data_dir_train,
                          subset="holdout",
                          no_savanna=args.no_savanna,
                          use_s2hr=args.use_s2hr,
                          use_s2mr=args.use_s2mr,
                          use_s2lr=args.use_s2lr,
                          use_s1=args.use_s1)
    else:
        #dfc2020_subset = args.dataset_val.split("_")[-1]
        val_set = DFC2020(args.data_dir_val,
                          subset=args.dataset_val,
                          no_savanna=args.no_savanna,
                          use_s2hr=args.use_s2hr,
                          use_s2mr=args.use_s2mr,
                          use_s2lr=args.use_s2lr,
                          use_s1=args.use_s1)
    # set up dataloaders
    train_loader = DataLoader(train_set,
                              batch_size=args.batch_size,
                              shuffle=True,
                              num_workers=args.workers,
                              pin_memory=True,
                              drop_last=False)
    val_loader = DataLoader(val_set,
                            batch_size=args.batch_size,
                            shuffle=False,
                            num_workers=args.workers,
                            pin_memory=True,
                            drop_last=False)

    # set up network
    if args.model == "deeplab":
        model = DeepLab(num_classes=n_classes,
                        backbone='resnet',
                        pretrained_backbone=args.pretrained_backbone,
                        output_stride=args.out_stride,
                        sync_bn=False,
                        freeze_bn=False,
                        n_in=n_inputs)
    else:
        model = UNet(n_classes=n_classes,
                     n_channels=n_inputs)

    if args.use_gpu:
        model = model.cuda()

    # define loss function
    loss_fn = nn.CrossEntropyLoss(ignore_index=255, reduction='mean')

    # set up optimizer
    if args.model == "deeplab":
        train_params = [{'params': model.get_1x_lr_params(),
                         'lr': args.lr},
                        {'params': model.get_10x_lr_params(),
                         'lr': args.lr * 10}]
        optimizer = torch.optim.SGD(train_params, momentum=args.momentum,
                                    weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.RMSprop(model.parameters(), lr=args.lr,
                                        weight_decay=args.weight_decay)

    # create checkpoint dir
    args.checkpoint_dir = os.path.join(args.log_dir, args.experiment_name,
                                       "checkpoints")
    os.makedirs(args.checkpoint_dir, exist_ok=True)

    # save config
    pkl.dump(args, open(os.path.join(args.checkpoint_dir, "args.pkl"), "wb"))

    # train network
    step = 0
    trainer = ModelTrainer(args)
    for epoch in range(args.max_epochs):
        print("="*20, "EPOCH", epoch + 1, "/", str(args.max_epochs), "="*20)

        # run training for one epoch
        model, step = trainer.train(model, train_loader, val_loader, loss_fn,
                                    optimizer, writer, step=step)

    # export final set of weights
    #trainer.export_model(model, args.checkpoint_dir, name="final")
    trainer.export_model(model, optimizer, name="final")


if __name__ == "__main__":
    main()