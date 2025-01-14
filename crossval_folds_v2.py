#Python2,3 compatible headers
from __future__ import unicode_literals,division
from builtins import int
from builtins import range

#System packages
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy
import math
import time
import random
import argparse
import sys
import os
import re
import copy
import importlib
from collections import namedtuple
from collections import OrderedDict
from itertools import chain

import util.db as db
import util.smartparse as smartparse
import util.file
import util.session_manager as session_manager
import dataloader

import sklearn.metrics
from hyperopt import hp, tpe, fmin

# Training settings
def default_params():
    params=smartparse.obj();
    #Data
    params.nsplits=4;
    params.pct=0.5
    #Model
    params.arch='arch.mlp_eig';
    params.data='data_r11_weight.pt';
    params.tag='';
    #MISC
    params.session_dir=None;
    params.budget=10000;
    return params

def create_session(params):
    session=session_manager.Session(session_dir=params.session_dir); #Create session
    torch.save({'params':params},session.file('params.pt'));
    pmvs=vars(params);
    pmvs=dict([(k,pmvs[k]) for k in pmvs if not(k=='stuff')]);
    print(pmvs);
    util.file.write_json(session.file('params.json'),pmvs); #Write a human-readable parameter json
    session.file('model','dummy');
    return session;


params = smartparse.parse()
params = smartparse.merge(params, default_params())
params.argv=sys.argv;

data=dataloader.new(params.data);
data.cuda();
params.stuff=data.preprocess();

import pandas
meta=pandas.read_csv('../trojai-datasets/cyber-pdf-dec2022-train/METADATA.csv');
meta_table={};
meta_table['model_name']=list(meta['model_name']);
meta_table['label']=[int(x) for x in list(meta['poisoned'])];
#meta_table['model_architecture']=list(meta['model_architecture']);
#meta_table['task_type']=list(meta['task_type']);

#meta_table['trigger_option']=list(meta['trigger_option']);
#meta_table['trigger_type']=list(meta['trigger_type']);
meta_table=db.Table(meta_table)
assert 'model_name' in data.data['table_ann'].d.keys()


data.data['table_ann']=db.left_join(data.data['table_ann'],meta_table,'model_name');

for k in data.data['table_ann'].d.keys():
    if isinstance(data.data['table_ann'][k],list):
        if len(data.data['table_ann'][k])>0 and torch.is_tensor(data.data['table_ann'][k][0]):
            print('sending to cuda')
            for i in range(len(data.data['table_ann'][k])):
                data.data['table_ann'][k][i]=data.data['table_ann'][k][i].cuda();



#precompute ws
arch=importlib.import_module(params.arch);
session=create_session(params);
params.session=session;

#Hyperparam search config
hp_config=[];

#   Architectures
#archs=['arch.mlpv2','arch.mlpv3','arch.mlpv4','arch.mlpv5','arch.mlpv6'];
archs=[params.arch];

hp_config.append(hp.choice('arch',archs));
hp_config.append(hp.qloguniform('nh',low=math.log(16),high=math.log(512),q=1));
hp_config.append(hp.qloguniform('nh2',low=math.log(16),high=math.log(512),q=1));
hp_config.append(hp.qloguniform('nh3',low=math.log(16),high=math.log(512),q=1));
hp_config.append(hp.quniform('nlayers',low=1,high=12,q=1));
hp_config.append(hp.quniform('nlayers2',low=1,high=12,q=1));
hp_config.append(hp.quniform('nlayers3',low=1,high=12,q=1));
hp_config.append(hp.loguniform('margin',low=math.log(2),high=math.log(1e1)));
#   OPT
hp_config.append(hp.qloguniform('epochs',low=math.log(3),high=math.log(500),q=1));
hp_config.append(hp.loguniform('lr',low=math.log(1e-5),high=math.log(1e-2)));
hp_config.append(hp.loguniform('decay',low=math.log(1e-8),high=math.log(1e-3)));
hp_config.append(hp.qloguniform('batch',low=math.log(8),high=math.log(64),q=1));

#Function to compute performance
def configure_pipeline(params,arch,nh,nh2,nh3,nlayers,nlayers2,nlayers3,margin,epochs,lr,decay,batch):
    params_=smartparse.obj();
    params_.arch=arch;
    params_.nh=int(nh);
    params_.nh2=int(nh2);
    params_.nh3=int(nh3);
    params_.nlayers=int(nlayers);
    params_.nlayers2=int(nlayers2);
    params_.nlayers3=int(nlayers3);
    params_.margin=margin;
    params_.epochs=int(epochs);
    params_.lr=lr;
    params_.decay=decay;
    params_.batch=int(batch);
    params_=smartparse.merge(params_,params);
    return params_;

crossval_splits=[];
#folds=[data.generate_random_crossval_split(pct=params.pct) for i in range(params.nsplits)];
folds=data.generate_crossval_folds(nfolds=params.nsplits);
folds+=data.generate_crossval_folds(nfolds=params.nsplits);
folds+=data.generate_crossval_folds(nfolds=params.nsplits);
folds+=data.generate_crossval_folds(nfolds=params.nsplits);
crossval_splits=[(data_train,data_test,data_test) for data_train,data_test in folds]


best_loss_so_far=1e10;
def run_crossval(p):
    global best_loss_so_far
    max_batch=16;
    arch,nh,nh2,nh3,nlayers,nlayers2,nlayers3,margin,epochs,lr,decay,batch=p;
    params_=configure_pipeline(params,arch,nh,nh2,nh3,nlayers,nlayers2,nlayers3,margin,epochs,lr,decay,batch);
    arch_=importlib.import_module(params_.arch);
    #Random splits N times
    t0=time.time();
    nets=[];
    for split_id,split in enumerate(crossval_splits):
        data_train,data_val,data_test=split;
        net=arch_.new(params_).cuda();
        opt=optim.Adam(net.parameters(),lr=params_.lr); #params_.lr
        
        #Training
        for iter in range(params_.epochs):
            #print('iter %d/%d'%(iter,params_.epochs))
            net.train();
            loss_total=[];
            for data_batch in data_train.batches(params_.batch,shuffle=True,full=True):
                opt.zero_grad();
                net.zero_grad();
                data_batch.cuda();
                C=data_batch['label'];
                data_batch.delete_column('label');
                scores_i=net(data_batch);
                
                #loss=F.binary_cross_entropy_with_logits(scores_i,C.float());
                spos=scores_i.gather(1,C.view(-1,1)).mean();
                sneg=torch.exp(scores_i).mean();
                loss=-(spos-sneg+1);
                l2=0;
                for p in net.parameters():
                    l2=l2+(p**2).sum();
                
                loss=loss+l2*params_.decay;
                loss.backward();
                loss_total.append(float(loss));
                opt.step();
            
            loss_total=sum(loss_total)/len(loss_total);
        
        #Temperature-scaling calibration on val
        net.eval();
        nets.append(net);
    
    #Calibration
    scores=[];
    gt=[];
    for split_id,split in enumerate(crossval_splits):
        data_train,data_val,data_test=split;
        net=nets[split_id];
        for data_batch in data_val.batches(max_batch):
            data_batch.cuda();
            
            C=data_batch['label'];
            data_batch.delete_column('label');
            scores_i=net.logp(data_batch);
            scores.append(scores_i.data);
            gt.append(C);
    
    scores=torch.cat(scores,dim=0);
    gt=torch.cat(gt,dim=0);
    
    T=torch.Tensor(1).fill_(0).cuda();
    T.requires_grad_();
    opt2=optim.Adamax([T],lr=3e-2);
    for iter in range(500):
        opt2.zero_grad();
        loss=F.binary_cross_entropy_with_logits(scores*torch.exp(-T),gt.float().cuda());
        loss.backward();
        opt2.step();
    
        
    #Eval & store
    scores=[];
    scores_pre=[];
    gt=[]
    model_id=[]
    ensemble=[];
    for split_id,split in enumerate(crossval_splits):
        data_train,data_val,data_test=split;
        net=nets[split_id];
        ensemble.append({'net':net.state_dict(),'params':params_,'T':float(T.data.cpu())})
        for data_batch in data_test.batches(max_batch):
            data_batch.cuda();
            
            C=data_batch['label'];
            data_batch.delete_column('label');
            scores_i=net.logp(data_batch);
            
            scores.append((scores_i*torch.exp(-T)).data.cpu());
            scores_pre.append(scores_i.data.cpu());
            model_id=model_id+data_batch['model_id'];
            
            gt.append(C.data.cpu());
    
    scores=torch.cat(scores,dim=0);
    scores_pre=torch.cat(scores_pre,dim=0);
    gt=torch.cat(gt,dim=0);
    
    def compute_metrics(scores,gt,keys=None):
        #Overall
        auc=float(sklearn.metrics.roc_auc_score(torch.LongTensor(gt).numpy(),torch.Tensor(scores).numpy()));
        #ce_=float(F.binary_cross_entropy_with_logits(torch.Tensor(scores),torch.Tensor(gt)));
        sgt=F.logsigmoid(torch.Tensor(scores)*(torch.Tensor(gt)*2-1))
        ce=-sgt.mean();
        cestd=sgt.std()/len(sgt)**0.5;
        return auc,ce,cestd;
    
    auc,ce,cestd=compute_metrics(scores.tolist(),gt.tolist());
    _,cepre,ceprestd=compute_metrics(scores_pre.tolist(),gt.tolist());
    mistakes=[];
    for i in range(len(gt)):
        if int(gt[i])==1 and float(scores[i])<=0:
            mistakes.append(model_id[i]);
    
    mistakes=sorted(mistakes);
    
    if float(cepre+0*ceprestd)<best_loss_so_far:
        best_loss_so_far=float(cepre+0*ceprestd);
        torch.save(ensemble,session.file('model.pt'))
    
    session.log('AUC: %f, CE: %f + %f, CEpre: %f + %f, time %.2f      (%s (%d,%d,%d), epochs %d, batch %d, lr %f, decay %f)'%(auc,ce,cestd,cepre,ceprestd,time.time()-t0,arch,nlayers,nlayers2,nh,epochs,batch,lr,decay));
    session.log('Mistakes: '+','.join(['%d'%i for i in mistakes]));
    print('\n')
    
    goal=float(cepre+0*ceprestd)
    
    return goal;



#Get results from hyper parameter search
best=fmin(run_crossval,hp_config,algo=tpe.suggest,max_evals=params.budget)
#best=util.macro.obj(best);
params_=configure_pipeline(**best);
hyper_params_str=json.dumps(best);
session.log('Best hyperparam (%s)'%(hyper_params_str));



#Load extracted features
#fvs_0=torch.load('fvs.pt');
#fvs_1=torch.load('fvs_1.pt');
#fvs=db.union(db.Table.from_rows(fvs_0),db.Table.from_rows(fvs_1));
#fvs.add_index('model_id');

#Load labels
#label=[];
#for i in range(200):
#    fname='/work/projects/trojai-example/data/trojai-round0-dataset/id-%08d/ground_truth.csv'%i;
#    f=open(fname,'r');
#    for line in f:
#        line.rstrip('\n').rstrip('\r')
#        label.append(int(line));
#        break;
#    
#    f.close();

#fvs['label']=label;
#data=db.DB({'table_ann':fvs});
#data.save('data.pt');
