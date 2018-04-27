import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import os
import time
import datetime
import sys
from torch.autograd import grad
from torch.autograd import Variable
from torchvision.utils import save_image
from torchvision import transforms
import tqdm
from PIL import Image
import ipdb
import config as cfg
import glob
import pickle
from utils import f1_score, f1_score_max, F1_TEST
import warnings
import xlsxwriter

warnings.filterwarnings('ignore')

class Solver(object):

  def __init__(self, rgb_loader, config, of_loader=None):
    # Data loader
    self.rgb_loader = rgb_loader

    #Optical Flow
    self.of_loader = of_loader
    self.of_loader_val=None
    self.OF=config.OF
    self.OF_option = config.OF_option

    self.image_size = config.image_size
    self.lr = config.lr
    self.beta1 = config.beta1
    self.beta2 = config.beta2

    # Training settings
    self.dataset = config.dataset
    self.num_epochs = config.num_epochs
    self.num_epochs_decay = config.num_epochs_decay
    self.batch_size = config.batch_size
    self.finetuning = config.finetuning
    self.pretrained_model = config.pretrained_model
    self.use_tensorboard = config.use_tensorboard
    self.stop_training = config.stop_training   

    # Test settings
    self.test_model = config.test_model
    self.metadata_path = config.metadata_path

    # Path
    self.log_path = config.log_path
    self.model_save_path = config.model_save_path
    self.fold = config.fold
    self.mode_data = config.mode_data
    self.xlsfile = config.xlsfile

    # Step size
    self.log_step = config.log_step

    #MISC
    self.GPU = config.GPU
    self.AU = config.AU

    # Build tensorboard if use
    self.build_model()
    if self.use_tensorboard:
      self.build_tensorboard()

    # Start with trained model
    if self.pretrained_model:
      self.load_pretrained_model()

  #=======================================================================================#
  #=======================================================================================#
  def build_model(self):
    # Define a generator and a discriminator

    from models.vgg16 import Classifier
    self.C = Classifier(pretrained=self.finetuning, OF_option=self.OF_option) 

    # Optimizer
    self.optimizer = torch.optim.Adam(self.C.parameters(), self.lr, [self.beta1, self.beta2])

    # Loss
    self.LOSS = nn.BCEWithLogitsLoss()
    # Print network
    self.print_network(self.C, 'Classifier')
    
    if torch.cuda.is_available():
      self.C.cuda()

  #=======================================================================================#
  #=======================================================================================#
  def print_network(self, model, name):
    num_params = 0
    for p in model.parameters():
      num_params += p.numel()
    # print(name)
    # print(model)
    print("The number of parameters: {}".format(num_params))

  #=======================================================================================#
  #=======================================================================================#
  def load_pretrained_model(self):
    model = os.path.join(
      self.model_save_path, '{}.pth'.format(self.pretrained_model))
    self.C.load_state_dict(torch.load(model))
    print('loaded trained model: {}!'.format(model))

  #=======================================================================================#
  #=======================================================================================#
  def build_tensorboard(self):
    from logger import Logger
    self.logger = Logger(self.log_path)

  #=======================================================================================#
  #=======================================================================================#
  def update_lr(self, lr):
    for param_group in self.optimizer.param_groups:
      param_group['lr'] = lr

  #=======================================================================================#
  #=======================================================================================#
  def reset_grad(self):
    self.optimizer.zero_grad()

  #=======================================================================================#
  #=======================================================================================#
  def to_var(self, x, volatile=False):
    if torch.cuda.is_available():
      x = x.cuda()
    return Variable(x, volatile=volatile)

  #=======================================================================================#
  #=======================================================================================#
  def threshold(self, x):
    x = x.clone()
    x = (x >= 0.5).float()
    return x

  #=======================================================================================#
  #=======================================================================================#
  def train(self):
    """Train StarGAN within a single dataset."""

    # Set dataloader

    # The number of iterations per epoch
    # ipdb.set_trace()
    iters_per_epoch = len(self.rgb_loader)

    # lr cache for decaying
    lr = self.lr
    
    # Start with trained model if exists
    if self.pretrained_model:
      if os.path.isfile(os.path.join(self.model_save_path, '{}_00.txt'.format(self.pretrained_model))):
        print("!!!Model already trained")
        return
      start = int(self.pretrained_model.split('_')[0])
      # Decay learning rate
      for i in range(start):
        if (i+1) > (self.num_epochs - self.num_epochs_decay):
          # g_lr -= (self.g_lr / float(self.num_epochs_decay))
          lr -= (self.lr / float(self.num_epochs_decay))
          self.update_lr(lr)
          print ('Decay learning rate to: {}.'.format(lr))      
    else:
      start = 0

    last_model_step = len(self.rgb_loader)

    print("Log path: "+self.log_path)

    Log = "[AUNets] OF:{}, bs:{}, AU:{}, fold:{}, GPU:{}, !{}, from:{}".format(self.OF_option, self.batch_size, str(self.AU).zfill(2), self.fold, self.GPU, self.mode_data, self.finetuning) 
    loss_cum = {}
    loss_cum['LOSS'] = []
    flag_init=True   

    loss_val_prev = 90
    f1_val_prev = 0
    non_decreasing = 0
    # Start training
    start_time = time.time()

    for e in range(start, self.num_epochs):
      E = str(e+1).zfill(2)
      self.C.train()

      if flag_init:
        f1_val, loss_val, f1_one = self.val(init=True)   
        log = '[F1_VAL: %0.3f (F1_VAL_1: %0.3f) LOSS_VAL: %0.3f]'%(f1_val, f1_one, loss_val)
        if self.pretrained_model: f1_val_prev=f1_val
        print(log)
        flag_init = False

      if self.OF:
        of_loader = iter(self.of_loader)
        print("--> RGB and OF # lines: %d - %d"%(len(self.rgb_loader), len(of_loader)))

      for i, (rgb_img, rgb_label, rgb_files) in tqdm.tqdm(enumerate(self.rgb_loader), \
          total=len(self.rgb_loader), desc='Epoch: %d/%d | %s'%(e,self.num_epochs, Log)):
        # ipdb.set_trace()
        rgb_img = self.to_var(rgb_img)
        rgb_label = self.to_var(rgb_label)
        if not self.OF:
          out = self.C(rgb_img)
        else:
          of_img, of_label, of_files = next(of_loader)
          if not of_label.eq(rgb_label.data.cpu()).all():
            print("OF and RGB must have the same labels")
            ipdb.set_trace()
          of_img = self.to_var(of_img)
          out = self.C(rgb_img, OF=of_img)
        # ipdb.set_trace()
        # loss_cls = F.cross_entropy(out, rgb_label.squeeze(1))
        loss_cls = self.LOSS(out, rgb_label)    

        # # Backward + Optimize
        self.reset_grad()
        loss_cls.backward()
        self.optimizer.step()


        # Logging
        loss = {}
        loss['LOSS'] = loss_cls.data[0]
        loss_cum['LOSS'].append(loss_cls.data[0])    
        # Print out log info
        if (i+1) % self.log_step == 0 or (i+1)==last_model_step:
          if self.use_tensorboard:
            for tag, value in loss.items():
              self.logger.scalar_summary(tag, value, e * iters_per_epoch + i + 1)



      #F1 val
      f1_val, loss_val = self.val()
      if self.use_tensorboard:
        self.logger.scalar_summary('F1_val: ', f1_val, e * iters_per_epoch + i + 1) 
        self.logger.scalar_summary('LOSS_val: ', loss_val, e * iters_per_epoch + i + 1)     

        for tag, value in loss_cum.items():
          self.logger.scalar_summary(tag, np.array(value).mean(), e * iters_per_epoch + i + 1)   
               
      #Stats per epoch
      elapsed = time.time() - start_time
      elapsed = str(datetime.timedelta(seconds=elapsed))      
      log = 'Elapsed: %s | [F1_VAL: %0.3f LOSS_VAL: %0.3f] | Train'%(elapsed, f1_val, loss_val)
      for tag, value in loss_cum.items():
        log += ", {}: {:.4f}".format(tag, np.array(value).mean())   

      print(log)

      # if loss_val<loss_val_prev:
      if f1_val>f1_val_prev:
        torch.save(self.C.state_dict(), os.path.join(self.model_save_path, '{}_{}.pth'.format(E, i+1)))   
        # loss_val_prev = loss_val
        print("! Saving model")
        f1_val_prev = f1_val
        non_decreasing = 0

      else:
        non_decreasing+=1
        if non_decreasing == self.stop_training:
          print("During {} epochs LOSS VAL was not decreasing.".format(self.stop_training))
          return            

      # Decay learning rate
      if (e+1) > (self.num_epochs - self.num_epochs_decay):
        # g_lr -= (self.g_lr / float(self.num_epochs_decay))
        lr -= (self.lr / float(self.num_epochs_decay))
        self.update_lr(lr)
        print ('Decay learning rate to: {}.'.format(lr))

  #=======================================================================================#
  #=======================================================================================#
  def val(self, init=False, load=False):
    """Facial attribute transfer on CelebA or facial expression synthesis on RaFD."""
    # Load trained parameters
    if init:
      from data_loader import get_loader
      # ipdb.set_trace()
      self.rgb_loader_val = get_loader(self.metadata_path, self.image_size,
                   self.image_size, self.batch_size, 'val')
      if self.OF:
        self.of_loader_val = get_loader(self.metadata_path, self.image_size,
             self.image_size, self.batch_size, 'val', OF=True)

      txt_path = os.path.join(self.model_save_path, '0_init_val.txt')

    if load:
      last_file = sorted(glob.glob(os.path.join(self.model_save_path,  '*.pth')))[-1]
      last_name = os.path.basename(config.test_model).split('.')[0]
      txt_path = os.path.join(self.model_save_path, '{}_{}_val.txt'.format(last_name,'{}'))
      try:
        output_txt  = sorted(glob.glob(txt_path.format('*')))[-1]
        number_file = len(glob.glob(output_txt))
      except:
        number_file = 0
      txt_path = txt_path.format(str(number_file).zfill(2)) 
    
      D_path = os.path.join(self.model_save_path, '{}.pth'.format(last_name))
      self.C.load_state_dict(torch.load(D_path))

    self.C.eval()

    if load: self.f=open(txt_path, 'a')  
    self.thresh = np.linspace(0.01,0.99,200).astype(np.float32)
    if not self.OF: self.of_loader_val=None
    f1,_,_,loss, f1_one = F1_TEST(self, self.rgb_loader_val, mode='VAL', OF=self.of_loader_val, verbose=load)
    if load: self.f.close()
    if init:
      return f1, loss, f1_one
    else:
      return f1, loss

  #=======================================================================================#
  #=======================================================================================#
  def test(self):
    """Facial attribute transfer on CelebA or facial expression synthesis on RaFD."""
    # Load trained parameters
    from data_loader import get_loader
    if self.test_model=='':
      last_file = sorted(glob.glob(os.path.join(self.model_save_path, '*.pth')))[-1]
      last_name = os.path.basename(last_file).split('.')[0]
    else:
      last_name = self.test_model

    D_path = os.path.join(self.model_save_path, '{}.pth'.format(last_name))
    txt_path = os.path.join(self.model_save_path, '{}_{}.txt'.format(last_name,'{}'))
    self.pkl_data = os.path.join(self.model_save_path, '{}_{}.pkl'.format(last_name, '{}'))
    print(" [!!] {} model loaded...".format(D_path))
    # self.G.load_state_dict(torch.load(G_path))
    self.C.load_state_dict(torch.load(D_path))
    self.C.eval()
    # ipdb.set_trace()
    data_loader_val = get_loader(self.metadata_path, self.image_size,
                   self.image_size, self.batch_size, 'val')
    data_loader_test = get_loader(self.metadata_path, self.image_size,
                   self.image_size, self.batch_size, 'test')

    if self.OF:
      of_loader_val = get_loader(self.metadata_path, self.image_size,
                     self.image_size, self.batch_size, 'val', OF=True)        
      of_loader_test = get_loader(self.metadata_path, self.image_size,
                   self.image_size, self.batch_size, 'test', OF=True)   

    if not hasattr(self, 'output_txt'):
      # ipdb.set_trace()
      self.output_txt = txt_path
      try:
        self.output_txt = sorted(glob.glob(self.output_txt.format('*')))[-1]
        number_file = len(glob.glob(self.output_txt))
      except:
        number_file = 0
      self.output_txt = self.output_txt.format(str(number_file).zfill(2)) 
    
    self.f=open(self.output_txt, 'a')  
    self.thresh = np.linspace(0.01,0.99,200).astype(np.float32)
    # ipdb.set_trace()
    if not self.OF:
      F1_real, F1_max, max_thresh_val,_, _  = F1_TEST(self, data_loader_val, mode = 'VAL')
      _ = F1_TEST(self, data_loader_test, thresh = max_thresh_val)
    else:
      F1_real, F1_max, max_thresh_val,_, _  = F1_TEST(self, data_loader_val, mode = 'VAL', OF=of_loader_val)
      _ = F1_TEST(self, data_loader_test, thresh = max_thresh_val, OF=of_loader_test)
   
    self.f.close()