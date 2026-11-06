"""Pixel share: neutral (HSV sat<0.12 or very dark/light) vs coloured. Also share of non-background coloured ink.
usage: colorshare.py img [x0 y0 x1 y1]"""
import sys
from PIL import Image
import numpy as np
im=Image.open(sys.argv[1]).convert('RGB')
if len(sys.argv)>2: im=im.crop(tuple(map(int,sys.argv[2:6])))
im.thumbnail((720,720))
a=np.asarray(im.convert('HSV')).astype(float)/255
s,v=a[...,1],a[...,2]
col=(s>=0.15)&(v>0.2)
rgb=np.asarray(im).reshape(-1,3)
vals,counts=np.unique(rgb,axis=0,return_counts=True)
bg=vals[counts.argmax()]
nonbg=(np.abs(np.asarray(im).astype(int)-bg).sum(-1)>24)
print(f"{sys.argv[1][:60]:60s} coloured={col.mean()*100:5.1f}%  coloured-of-ink={(col&nonbg).sum()/max(nonbg.sum(),1)*100:5.1f}%  ink={nonbg.mean()*100:5.1f}%")
