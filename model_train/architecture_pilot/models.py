"""Fixed architecture comparison; input B x 150 x 51, no augmentation."""
import numpy as np
import torch
from torch import nn
from model_train.robust.shared_temporal import SharedEncoder

NAMES = ('shared', 'cnn', 'gru', 'lstm', 'transformer', 'rf')

class Shared(SharedEncoder):
    def __init__(self):
        super().__init__(mode='A')
    def forward(self, x):
        b,t,f=x.shape
        h=self.encoder(x.transpose(1,2).reshape(b*f,1,t))
        series=torch.cat([h.mean(2),h.std(2,unbiased=False)],1).reshape(b,f,32)
        return self.head(torch.cat([series.mean(1),series.std(1,unbiased=False),series.amax(1)],1))

class Temporal(nn.Module):
    def __init__(self, kind):
        super().__init__(); self.kind=kind
        if kind=='cnn':
            layers=[]
            for i,(a,b,k) in enumerate([(51,32,5),(32,64,5),(64,64,3)]):
                layers += [nn.Conv1d(a,b,k,padding=k//2,bias=False),nn.BatchNorm1d(b),nn.ReLU()]
                if i<2: layers.append(nn.AvgPool1d(2))
            self.body=nn.Sequential(*layers)
        elif kind in ('gru','lstm'):
            self.body=(nn.GRU if kind=='gru' else nn.LSTM)(51,64,2,batch_first=True,dropout=.1)
        else:
            self.project=nn.Linear(51,64)
            pos=torch.arange(150).float()[:,None]; div=torch.exp(torch.arange(0,64,2)*(-np.log(10000)/64))
            pe=torch.zeros(150,64);pe[:,0::2]=torch.sin(pos*div);pe[:,1::2]=torch.cos(pos*div)
            self.register_buffer('position',pe)
            self.body=nn.TransformerEncoder(nn.TransformerEncoderLayer(64,4,128,.1,batch_first=True),2,enable_nested_tensor=False)
        self.head=nn.Sequential(nn.Linear(128,32),nn.ReLU(),nn.Dropout(.3),nn.Linear(32,3))
    def forward(self,x):
        if self.kind=='cnn': h=self.body(x.transpose(1,2)).transpose(1,2)
        elif self.kind in ('gru','lstm'): h=self.body(x)[0]
        else: h=self.body(self.project(x)+self.position)
        return self.head(torch.cat([h.mean(1),h.std(1,unbiased=False)],1))

def make(kind): return Shared() if kind=='shared' else Temporal(kind)
def statistics(x):
    return np.concatenate([x.mean(1),x.std(1),x.min(1),x.max(1),np.abs(np.diff(x,axis=1)).mean(1)],1)
