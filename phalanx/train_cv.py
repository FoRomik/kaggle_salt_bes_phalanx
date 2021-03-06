import os
import argparse
import numpy as np
import pandas as pd

from utils import do_kaggle_metric, get_model
from lovasz_losses import lovasz_hinge, lovasz_hinge2
from salt_dataset import SaltDataset, trainImageFetch, semi_trainImageFetch
from unet_model import Res34Unetv3, Res34Unetv4, Res34Unetv5

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.data.sampler import RandomSampler

# fastprogress available only for Python3.6+ and Tensorflow Docker image for Python < 3.6
# from fastprogress import master_bar, progress_bar

parser = argparse.ArgumentParser(description='')
parser.add_argument('--model', default='res34v5', type=str, help='Model version')
parser.add_argument('--fine_size', default=101, type=int, help='Resized image size')
parser.add_argument('--pad_left', default=13, type=int, help='Left padding size')
parser.add_argument('--pad_right', default=14, type=int, help='Right padding size')
parser.add_argument('--batch_size', default=64, type=int, help='Batch size for training')
parser.add_argument('--epoch', default=300, type=int, help='Number of training epochs')
parser.add_argument('--snapshot', default=5, type=int, help='Number of snapshots per fold')
parser.add_argument('--cuda', default=True, type=bool, help='Use cuda to train model')
parser.add_argument('--save_weight', default='weights/', type=str, help='weight save space')
parser.add_argument('--max_lr', default=0.01, type=float, help='max learning rate')
parser.add_argument('--min_lr', default=0.001, type=float, help='min learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum for SGD')
parser.add_argument('--weight_decay', default=1e-4, type=float, help='Weight decay for SGD')
parser.add_argument('--is_pseudo', default=False, type=bool, help='Use pseudolabels or not')

args = parser.parse_args()
fine_size = args.fine_size + args.pad_left + args.pad_right
args.weight_name = 'model_' + str(fine_size) + '_' + args.model

if not os.path.isdir(args.save_weight):
    os.mkdir(args.save_weight)

device = torch.device('cuda' if args.cuda else 'cpu')

all_id = pd.read_csv('../data/train.csv')['id'].values
df = pd.read_csv('../data/train_proc_v2_gr.csv')
fold = []
for i in range(5):
    fold.append(df[df['fold'] == i]['id'].values)


def test(test_loader, model):
    running_loss = 0.0
    predicts = []
    truths = []

    model.eval()
    for inputs, masks in test_loader:
        inputs, masks = inputs.to(device), masks.to(device)
        with torch.set_grad_enabled(False):
            if args.is_pseudo:
                outputs, _, _ = model(inputs)
            else:
                outputs = model(inputs)

            outputs = outputs[:, :, args.pad_left:args.pad_left + args.fine_size,
                      args.pad_left:args.pad_left + args.fine_size].contiguous()
            loss = lovasz_hinge(outputs.squeeze(1), masks.squeeze(1))

        predicts.append(F.sigmoid(outputs).detach().cpu().numpy())
        truths.append(masks.detach().cpu().numpy())
        running_loss += loss.item() * inputs.size(0)

    predicts = np.concatenate(predicts).squeeze()
    truths = np.concatenate(truths).squeeze()
    precision, _, _ = do_kaggle_metric(predicts, truths, 0.5)
    precision = precision.mean()
    epoch_loss = running_loss / val_data.__len__()
    return epoch_loss, precision


def train(train_loader, model):
    running_loss = 0.0
    data_size = train_data.__len__()

    model.train()
    # for inputs, masks, labels in progress_bar(train_loader, parent=mb):
    for inputs, masks, labels in train_loader:
        inputs, masks, labels = inputs.to(device), masks.to(device), labels.to(device)
        optimizer.zero_grad()

        with torch.set_grad_enabled(True):
            if args.is_pseudo:
                logit, logit_pixel, logit_image = model(inputs)
                loss1 = lovasz_hinge(logit.squeeze(1), masks.squeeze(1))
                loss2 = nn.BCELoss()(logit_image, labels)
                loss3 = lovasz_hinge2(logit_pixel.squeeze(1), masks.squeeze(1))
                loss = loss1 + loss2 + loss3
            else:
                logit = model(inputs)
                loss = lovasz_hinge(logit.squeeze(1), masks.squeeze(1))

            loss.backward()
            optimizer.step()

        running_loss += loss.item() * inputs.size(0)
        # mb.child.comment = 'loss: {}'.format(loss.item())
    epoch_loss = running_loss / data_size
    return epoch_loss


if __name__ == '__main__':
    scheduler_step = args.epoch // args.snapshot
    # Get Model
    salt = get_model(args.model)
    salt.to(device)
    if args.model == 'res34v5':
        param = torch.load(
            args.save_weight + args.weight_name + '.pth')  # stage3 use model pretrained with pseudo-labels

    for idx in range(5):
        if args.model == 'res34v5':
            salt.load_state_dict(param)  # initialize with pretained weight

        # Setup optimizer
        optimizer = torch.optim.SGD(salt.parameters(), lr=args.max_lr, momentum=args.momentum,
                                    weight_decay=args.weight_decay)
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, scheduler_step, args.min_lr)

        # Load data
        train_id = np.setdiff1d(all_id, fold[idx])
        val_id = fold[idx]
        X_train, y_train = trainImageFetch(train_id)
        X_val, y_val = trainImageFetch(val_id)

        # Load pseudo labels for stage2
        if args.is_pseudo:
            pseudo_id = np.load('../data/stage2_fold' + str(idx) + '.npy')
            X_pseudo, y_pseudo = semi_trainImageFetch('../data/pseudolabels/', pseudo_id)
            X_train = np.concatenate((X_train, X_pseudo))
            y_train = np.concatenate((y_train, y_pseudo))

        train_data = SaltDataset(X_train, mode='train', mask_list=y_train, fine_size=args.fine_size, pad_left=args.pad_left,
                                 pad_right=args.pad_right)
        train_loader = DataLoader(
                            train_data,
                            shuffle=RandomSampler(train_data),
                            batch_size=args.batch_size,
                            num_workers=8,
                            pin_memory=True)
        
        val_data = SaltDataset(X_val, mode='val', mask_list=y_val, fine_size=args.fine_size, pad_left=args.pad_left,
                               pad_right=args.pad_right)
        val_loader = DataLoader(
                            val_data,
                            shuffle=False,
                            batch_size=args.batch_size,
                            num_workers=8,
                            pin_memory=True)

        num_snapshot = 0
        best_acc = 0

        # mb = master_bar(range(args.epoch))
        # for epoch in mb:
        for epoch in range(args.epoch):
            train_loss = train(train_loader, salt)
            val_loss, accuracy = test(val_loader, salt)
            lr_scheduler.step()

            if accuracy > best_acc:
                best_acc = accuracy
                best_param = salt.state_dict()

            if (epoch + 1) % scheduler_step == 0:
                torch.save(best_param, args.save_weight + args.weight_name + str(idx) + str(num_snapshot) + '.pth')
                optimizer = torch.optim.SGD(salt.parameters(), lr=args.max_lr, momentum=args.momentum,
                                            weight_decay=args.weight_decay)
                lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, scheduler_step, args.min_lr)
                num_snapshot += 1
                best_acc = 0

            # mb.write('epoch: {} train_loss: {:.3f} val_loss: {:.3f} val_accuracy: {:.3f}'.format(epoch + 1, train_loss,
            #                                                                                     val_loss, accuracy))
            print('epoch: {} train_loss: {:.3f} val_loss: {:.3f} val_accuracy: {:.3f}'.format(epoch + 1, train_loss,
                                                                                              val_loss, accuracy))
