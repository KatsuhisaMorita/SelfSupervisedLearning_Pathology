# -*- coding: utf-8 -*-
"""
# barlowtwins with recursive resuming

@author: Katsuhisa MORITA
"""
# path setting
PROJECT_PATH = '/workspace/pathology'

# packages installed in the current environment
import sys
import os
import gc
import datetime
import argparse
import time
import random

import numpy as np
import pandas as pd
from tqdm import tqdm, trange
from sklearn.metrics import roc_auc_score
from sklearn.metrics import accuracy_score

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
from PIL import Image
from torch.utils.data import Dataset

# original packages in src
sys.path.append(f"{PROJECT_PATH}/src/SelfSupervisedLearningPathology")
import sslmodel
from sslmodel import data_handler as dh
import sslmodel.sslutils as sslutils
import tggate.utils as utils

# argument
parser = argparse.ArgumentParser(description='CLI learning')
# base settings
parser.add_argument('--note', type=str, help='barlowtwins running')
parser.add_argument('--seed', type=int, default=0)
# data settings
parser.add_argument('--fold', type=int, default=0) # number of fold
parser.add_argument('--num_comp', type=int, default=100) # number of WSI
parser.add_argument('--dir_result', type=str, help='result')
# model/learning settings
parser.add_argument('--model_name', type=str, default='ResNet18') # model architecture name
parser.add_argument('--ssl_name', type=str, default='barlowtwins') # ssl architecture name
parser.add_argument('--num_epoch', type=int, default=50) # epoch
parser.add_argument('--batch_size', type=int, default=64) # batch size
parser.add_argument('--lr', type=float, default=0.01) # learning rate
parser.add_argument('--patience', type=int, default=3) # early stopping
parser.add_argument('--delta', type=float, default=0.002) # early stopping
parser.add_argument('--resume_epoch', type=int, default=20) # max repeat epoch for one run
parser.add_argument('--resume', action='store_true') # resuming or not
parser.add_argument('--resize', action='store_true') # resuming or not
# Transform (augmentation) settings
parser.add_argument('--color_plob', type=float, default=0.8)
parser.add_argument('--blur_plob', type=float, default=0.4)
parser.add_argument('--solar_plob', type=float, default=0.)

args = parser.parse_args()
sslmodel.utils.fix_seed(seed=args.seed, fix_gpu=True) # for seed control

# prepare data
class Dataset_Batch(torch.utils.data.Dataset):
    """ load for each version """
    def __init__(self,
                batch_number:int=None,
                transform=None,
                num_comp="",
                fold:int=None,
                ):
        # set transform
        if type(transform)!=list:
            self._transform = [transform]
        else:
            self._transform = transform
        # load data
        with open(f"/workspace/HDD3/TGGATEs/batch_comp/fold{fold}_n{num_comp}_{batch_number}.npy", 'rb') as f:
            self.data = np.load(f)
        self.datanum = len(self.data)
        gc.collect()

    def __len__(self):
        return self.datanum

    def __getitem__(self,idx):
        out_data = self.data[idx]
        out_data = Image.fromarray(out_data).convert("RGB")
        if self._transform:
            for t in self._transform:
                out_data = t(out_data)
        return out_data

def prepare_data(batch_number:int=0, num_comp:int=0, batch_size:int=32, fold:int=0):
    """
    data preparation
    
    """
    # normalization
    train_transform = ssl_class.prepare_transform(
        color_plob=args.color_plob, blur_plob=args.blur_plob, solar_plob=args.solar_plob,
    )
    # data
    train_dataset = Dataset_Batch(
        batch_number=batch_number,
        num_comp=num_comp,
        transform=train_transform,
        fold=fold,
        )
    # resize for test
    if args.resize:
        train_dataset = dh.resize_dataset(train_dataset, size=128)
    # to loader
    train_loader = dh.prep_dataloader(train_dataset, batch_size)
    return train_loader

# train epoch
def train_epoch(model, criterion, optimizer, epoch):
    """
    train for epoch
    with minibatch
    """
    model.train() # training
    train_batch_loss = []
    # define loader
    dict_batch={
        5:1,
        10:2,
        15:3,
        20:4,
    }
    lst_minibatch=list(range(dict_batch[args.num_comp]))
    random.seed(args.seed+epoch)
    random.shuffle(lst_minibatch)
    random.seed(args.seed)
    for batch_number in lst_minibatch:
        # prep data
        train_loader=prepare_data(batch_number=batch_number, num_comp=args.num_comp, batch_size=args.batch_size, fold=args.fold)
        # train
        for data in train_loader:
            loss = ssl_class.calc_loss(
                model, data, criterion,
            )
            train_batch_loss.append(loss.item())
            optimizer.zero_grad() # reset gradients
            loss.backward() # backpropagation
            optimizer.step() # update parameters
        del train_loader
        gc.collect()
    return model, np.mean(train_batch_loss)

# train
def train(model, criterion, optimizer, scheduler, early_stopping, num_epoch:int=100, epoch_start:int=0, train_loss=list()):
    """ train ssl model """
    # settings
    start = time.time() # for time stamp
    for epoch in range(epoch_start, num_epoch):
        # train
        model, train_epoch_loss = train_epoch(model, criterion, optimizer, epoch)
        scheduler.step()
        train_loss.append(train_epoch_loss)
        LOGGER.logger.info(
            f'Epoch: {epoch + 1}, train_loss: {train_epoch_loss:.4f}'
            )
        LOGGER.logger.info('elapsed_time: {:.2f} min'.format((time.time() - start)/60))
        # save model
        state = {
            "epoch":epoch,
            "model_state_dict":model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
            "criterion":criterion,
            "early_stopping":early_stopping,
            "train_loss":train_loss
        }
        torch.save(state, f'{DIR_NAME}/state.pt')
        LOGGER.save_logger(fileout=file_log)
        # state check
        ## early stopping
        early_stopping(train_epoch_loss, model)
        if early_stopping.early_stop:
            LOGGER.logger.info(f'Early Stopping with Epoch: {epoch}')
            model.load_state_dict(torch.load(early_stopping.path))        
            return model, train_loss, True
        ## time limit
        if epoch==epoch_start+args.resume_epoch-1:
            return None, None, False
    return model, train_loss, True

def main(resume=False):
    # 1. Preparing
    if resume:
        model, criterion, optimizer, scheduler, early_stopping, train_loss, epoch = utils.load_model_train(
            model_name=args.model_name, ssl_name=args.ssl_name,
            patience=args.patience, delta=args.delta, lr=args.lr, num_epoch=args.num_epoch,
            DIR_NAME=DIR_NAME, DEVICE=DEVICE
        )
        epoch_start=epoch+1
    else:
        model, criterion, optimizer, scheduler, early_stopping = utils.prepare_model_train(
            model_name=args.model_name, ssl_name=args.ssl_name,
            patience=args.patience, delta=args.delta, lr=args.lr, num_epoch=args.num_epoch,
            DIR_NAME=DIR_NAME, DEVICE=DEVICE,
        )
        epoch_start=0
        train_loss=[]
    # 2. Training
    model, train_loss, flag_finish = train(
        model, criterion, optimizer, scheduler, early_stopping, num_epoch=args.num_epoch, epoch_start=epoch_start, train_loss=train_loss,
    )        
    # 3. save results & config
    if flag_finish:
        sslmodel.plot.plot_progress_train(train_loss, DIR_NAME)
        sslmodel.utils.summarize_model(
            model,
            None,
            DIR_NAME, lst_name=['summary_ssl.txt', 'model_ssl.pt']
        )
        LOGGER.to_logger(name='argument', obj=args)
        LOGGER.to_logger(name='loss', obj=criterion)
        LOGGER.to_logger(
            name='optimizer', obj=optimizer, skip_keys={'state', 'param_groups'}
        )
        LOGGER.to_logger(name='scheduler', obj=scheduler)
    else:
        LOGGER.logger.info('reached max epoch / train')

if __name__ == '__main__':
    filename = os.path.basename(__file__).split('.')[0]
    DIR_NAME = PROJECT_PATH + '/result/' +args.dir_result # for output
    file_log = f'{DIR_NAME}/logger.pkl'
    now = datetime.datetime.now().strftime('%H%M%S')
    DEVICE = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu') # get device
    # Set SSL class
    ssl_class=utils.DICT_SSL[args.ssl_name](DEVICE=DEVICE)
    if args.resume:
        if not os.path.exists(file_log):
            print("log file doesnt exist")
        LOGGER = sslmodel.utils.logger_save()
        LOGGER.load_logger(filein=file_log)
        LOGGER.logger.info(f"resume: {now}")
        main(resume=True)
    else:
        if not os.path.exists(DIR_NAME):
            os.makedirs(DIR_NAME)
        LOGGER = sslmodel.utils.logger_save()
        LOGGER.init_logger(filename, DIR_NAME, now, level_console='debug') 
        main(resume=False)
