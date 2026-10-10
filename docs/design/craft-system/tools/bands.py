"""Ink bands in a crop: prints rows/cols spans where pixels differ from the crop's background.
usage: bands.py img x0 y0 x1 y1 [scale] [axis=y|x] [thresh]"""
import sys
from PIL import Image
import numpy as np
img=sys.argv[1]; x0,y0,x1,y1=map(int,sys.argv[2:6])
scale=float(sys.argv[6]) if len(sys.argv)>6 else 1.0
axis=sys.argv[7] if len(sys.argv)>7 else 'y'
th=float(sys.argv[8]) if len(sys.argv)>8 else 40
a=np.asarray(Image.open(img).convert('L').crop((x0,y0,x1,y1))).astype(int)
# background = mode
vals,counts=np.unique(a,return_counts=True); bg=vals[counts.argmax()]
ink=np.abs(a-bg)>th
prof=ink.any(axis=1) if axis=='y' else ink.any(axis=0)
off=y0 if axis=='y' else x0
runs=[];start=None
for i,v in enumerate(prof):
    if v and start is None: start=i
    if not v and start is not None: runs.append((start,i));start=None
if start is not None: runs.append((start,len(prof)))
prev=None
for s,e in runs:
    gap=(s-prev) if prev is not None else 0
    print(f"{off+s:5d}-{off+e:5d} h={e-s:4d} ({(e-s)/scale:5.1f}css) gap={gap/scale:5.1f}css")
    prev=e
print('bg',bg)
