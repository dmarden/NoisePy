import os
import gc
import sys
import glob
import numpy as np
import scipy
import obspy
import matplotlib.pyplot as plt
from datetime import datetime
import noise_module
import time
import pyasdf
import pandas as pd
from mpi4py import MPI

if not sys.warnoptions:
    import warnings
    warnings.simplefilter("ignore")

'''
Step2 of the NoisePy package.

This script loops through days by using MPI and compute cross-correlation functions for each
station-pair at that day.
by C.Jiang, M.Denolle, T.Clements (Nov.09.2018)

Update history:
    - achieve ~5 times speed-up by 1) making smoothed spectrum of the source outside of the 
    receiver loop; 2) taking advantage of the linearality of ifft to average the spectrum 
    first before doing ifft to get cross-correlaiton functions, and 3) sacrifice storage (~1.5 times)
    to improve the I/O speed (by 4 times). 
    Thanks to Zhitu Ma for thoughtful discussions.  (Jan,28,2019)

    - new updates to 1) remove the need of input station.lst file by make list of available HDF5 files;
    2) make use of the inventory for lon, lat information, 3) add new parameters to HDF5 files needed
    for later CC steps and 4) make data_types and paths in the same format (Feb.15.2019). 

    - modify the structure of ASDF files to make it more flexable for later stacking and matrix rotation
    (Mar.06.2019)

    - add a free parameter (integer times of cc_len) to keep sub-stackings of the daily ccfs. it also
    tries to load as much fft data at once as possible to save repetitive data loading time (Jun.16.2019)

    - load useful parameters directly from saved file so that it avoids parameters re-difination (Jun.17.2019)

    - cast intermediate steps into sub-functions to increase clear logic flow (Jun.29.2019)

Note:
    !!!!!!VERY IMPORTANT!!!!!!!!
    As noted in S1, we choose 1-day as the basic length for data storage and processing but allow breaking 
    the daily chunck data to smaller length for pre-processing and fft. In default, we choose to average 
    all of the CCFs for the day (e.g., betwen every hour). the variable of sub_stack_len is to keep
    sub-stacks of the day, which might be useful if your target time-scale is on the order of hours. if you
    want to have longer duration however, (e.g., 10 days), then you need to check out S3.  
'''

ttt0=time.time()

########################################
#########PARAMETER SECTION##############
########################################

#------load most parameters from fft metadata files-----
rootpath  = '/mnt/data0/NZ/XCORR'
f_metadata = os.path.join(rootpath,'fft_metadata.txt')
c_metadata = os.path.join(rootpath,'cc_metadata.txt')
if not os.path.isfile(f_metadata):
    raise ValueError('Abort! cannot find metadata file used for fft %s' % f_metadata)
fft_para = eval(open(f_metadata).read())

#----absolute paths for new inputs/outputs-----
FFTDIR   = fft_para['FFTDIR']
CCFDIR   = os.path.join(rootpath,'CCF')
if not os.path.isdir(CCFDIR):os.mkdir(CCFDIR)

# load useful parameters from fft_para
dt     = fft_para['dt']
cc_len = fft_para['cc_len']
step   = fft_para['step']
maxlag = fft_para['maxlag']             # enlarge this number if to do C3
cc_method  = fft_para['cc_method']             # selected in S1
start_date = fft_para['start_date']
end_date   = fft_para['end_date']
inc_days   = fft_para['inc_days']

# some new control parameters for cc
flag           = False                      # output intermediate variables and computing times
auto_corr      = False                      # include single-station cross-correlation or not
substack       = False                      # sub-stack daily cross-correlation or not
substack_len   = 4*cc_len                   # Time unit in sectons to stack over: need to be integer times of cc_len
smoothspect_N  = 10                         # moving window length to smooth spectrum amplitude

# reset cc paramters if needed
start_date = '2010_01_01'                   # these two variables allow processing subset of the continuous noise data
end_date   = '2010_01_02'
inc_days   = 1                              # this has to be 1 because it is the basic length we use (see NOTES above)

# criteria for data selection
max_over_std = 10                           # maximum threshold between the maximum absolute amplitude and the STD of the time series
max_kurtosis = 10                           # max kurtosis allowed.

# maximum memory allowed per core in Gb.
MAX_MEM = 4.0

#-----make a dictionary to store all variables-----
cc_para={'dt':dt,'cc_len':cc_len,'step':step,'cc_method':cc_method,'maxlag':maxlag,\
    'smoothspect_N':smoothspect_N,'substack':substack,'substack_len':substack_len,\
    'start_date':start_date,'end_date':end_date,'inc_days':inc_days,'max_over_std':\
    max_over_std,'max_kurtosis':max_kurtosis,'MAX_MEM':MAX_MEM}

#--save cc metadata for later use--
fout = open(c_metadata,'w')
fout.write(str(cc_para))
fout.close()

#######################################
###########PROCESSING SECTION##########
#######################################

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

#-------form a station pair to loop through-------
if rank ==0:
    if not os.path.isdir(CCFDIR):
        os.mkdir(CCFDIR)

    #------the station order should be kept here--------
    sfiles = sorted(glob.glob(os.path.join(FFTDIR,'*.h5')))
    day = noise_module.get_event_list(start_date,end_date,inc_days)
    splits = len(day)

    if not sfiles:
        raise IOError('Abort! No FFT data in %s' % FFTDIR)
else:
    splits,sfiles,day = [None for _ in range(3)]

#------split the common variables------
splits = comm.bcast(splits,root=0)
day    = comm.bcast(day,root=0)
sfiles   = comm.bcast(sfiles,root=0)
extra  = splits % size

for ii in range(rank,splits+size-extra,size):

    if ii<splits:
        iday = day[ii]

        tt0=time.time()

        #-----------get parameters of Nfft and Nseg--------------
        with pyasdf.ASDFDataSet(sfiles[0],mpi=False,mode='r') as ds:
            data_types = ds.auxiliary_data.list()
            paths      = ds.auxiliary_data[data_types[0]].list()
            Nfft  = ds.auxiliary_data[data_types[0]][paths[0]].parameters['nfft']
            Nseg  = ds.auxiliary_data[data_types[0]][paths[0]].parameters['nseg']

        #----double check ncomp by opening a few more stations------
        for jj in range(1,5):
            with pyasdf.ASDFDataSet(sfiles[jj],mpi=False,mode='r') as ds:
                data_types = ds.auxiliary_data.list()
                if len(data_types) > ncomp:
                    ncomp = len(data_types)
                    print('first station of %s misses other components' % (sfiles[0]))

        # important parameters about data matrix
        ncomp = len(data_types)
        nsta  = len(sfiles)
        ntrace= ncomp*nsta
        Nfft2 = Nfft//2
        
        # crutial estimate on required memory for loading all FFT at once: float32 in default
        num_load  = 1
        nseg2load = Nseg
        memory_size = ntrace*Nfft2*Nseg*8/1024/1024/1024
        if memory_size > MAX_MEM:
            print('Memory exceeds %s GB! No enough memory to load them all once!' % (MAX_MEM))
            nseg2load = np.floor(MAX_MEM/(ntrace*Nfft2*8/1024/1024/1024 ))
            num_load= np.floor(Nseg/nseg2load)
            print('thus splitting the files into %s chunks of %3.1f GB each' % (num_load, memory_size))

        # make a dictionary here to load data
        data_dic = {'nsta':nsta,'num_load':num_load,'nseg2load':nseg2load,'ntrace':ntrace,\
            'Nfft':Nfft,'Nfft2':Nfft2,'Nseg':Nseg,'ncomp':ncomp}

        #--------load data for sub-stacking-------
        for iload in range(num_load):

            t0 = time.time()
            fft_array,fft_std,fft_flag,Timestamps,sta_info = noise_module.load_fft_matrix(iday,iload,data_dic,sfiles,flag)
            t1 = time.time()

            if flag:
                print('loading all FFT takes %6.4fs' % (t1-t0))

            #-------loop I of each source------
            for isource in range(nsta-1):

                #---station info---
                staS = sta_info['sta'][isource]
                netS = sta_info['net'][isource]
                lonS = sta_info['lon'][isource]
                latS = sta_info['lat'][isource]

                if flag:
                    print('source: %s %s' % (staS,netS))

                #-----loop II of each component------
                for icompS in range(ncomp):
                    cc_indxS = isource*ncomp+icompS

                    #---no data for icomp---
                    if fft_flag[cc_indxS]==0:
                        print('no data for %dth comp of %s' %(icompS,staS))
                        continue
                            
                    fft1 = fft_array[cc_indxS][:]
                    source_std = fft_std[cc_indxS][:]
                    
                    t0=time.time()
                    #-----------get the smoothed source spectrum for decon later----------
                    sfft1 = noise_module.smooth_source_spect(cc_para,fft1)
                    sfft1 = sfft1.reshape(nseg2load,Nfft2)
                    t1=time.time()
                    if flag:
                        print('smooth takes %6.4fs' % (t1-t0))

                    #-----------now loop III for each receiver B----------
                    for ireceiver in range(isource+1,nsta):

                        #---station info---
                        staR = sta_info['sta'][ireceiver]
                        netR = sta_info['net'][ireceiver]
                        lonR = sta_info['lon'][ireceiver]
                        latR = sta_info['lat'][ireceiver]

                        if flag:
                            print('receiver: %s %s' % (staR,netR))

                        #--------loop IV of each component-------
                        for icompR in range(ncomp):
                            cc_indxR = ireceiver*ncomp+icompR

                            #---no data for icomp---
                            if fft_flag[cc_indxR]==0:
                                #print('no data for %dth comp of %s' %(icompR,staR))
                                continue
                            
                            t2 = time.time()
                            fft2 = fft_array[cc_indxR][:]
                            fft2 = fft2.reshape(nseg2load,Nfft2)
                            receiver_std = fft_std[cc_indxR][:]

                            #---------- check the existence of earthquakes ----------
                            rec_ind = np.where( (receiver_std < cc_para['max_over_std']) & (receiver_std> 0) \
                                & (np.isnan(receiver_std)==0))[0]
                            sou_ind = np.where( (source_std < cc_para['max_over_std'])   & (source_std > 0)  \
                                & (np.isnan(source_std)==0))[0]

                            #-----note that Hi-net has a few mi-secs differences to Mesonet in terms starting time-----
                            bb=np.intersect1d(sou_ind,rec_ind)
                            if len(bb)==0:continue

                            t3=time.time()
                            corr,tcorr,ncorr=noise_module.optimized_correlate(sfft1[bb,:],\
                                fft2[bb,:],cc_para,Nfft,Timestamps[cc_indxR][bb])
                            t4=time.time()

                            #---------------keep daily cross-correlation into a hdf5 file--------------
                            cc_aday_h5 = os.path.join(CCFDIR,iday+'.h5')
                            crap   = np.zeros(corr.shape,dtype=np.float32)

                            if not os.path.isfile(cc_aday_h5):
                                with pyasdf.ASDFDataSet(cc_aday_h5,mpi=False) as ccf_ds:
                                    pass 

                            with pyasdf.ASDFDataSet(cc_aday_h5,mpi=False) as ccf_ds:
                                coor = {'lonS':lonS,'latS':latS,'lonR':lonR,'latR':latR}
                                parameters = noise_module.optimized_cc_parameters(cc_para,coor,tcorr,ncorr)              # keep the timestamp of each sub-stack

                                #-----------make a universal change to component-----------
                                if icompR==0:  compR = 'E'
                                elif icompR==1:compR = 'N'
                                elif icompR==2:compR = 'Z' 

                                if icompS==0:  compS = 'E'
                                elif icompS==1:compS = 'N'
                                elif icompS==2:compS = 'Z' 

                                #------save the time domain cross-correlation functions-----
                                path = netR+'s'+staR+'s'+compR+str(iload)
                                data_type = netS+'s'+staS+'s'+compS

                                crap[:] = corr[:]
                                ccf_ds.add_auxiliary_data(data=crap, data_type=data_type, path=path, parameters=parameters)

                            t5=time.time()
                            if flag:
                                print('read R %6.4fs, cc %6.4fs, write cc %6.4fs'% ((t3-t2),(t4-t3),(t5-t4)))

            fft_array=[];fft_std=[];fft_flag=[];Timestamps=[]
            n = gc.collect();print('unreadable garbarge',n)

            t6 = time.time()
            if flag:print('it takes %6.4fs to process %dth segment of data' %((t6-t0),iload))

        tt1 = time.time()
        if flag:print('it takes %6.4fs to process day %s [%d segment] in step 2' % (tt1-tt0,iday,num_load))


ttt1=time.time()
print('all step 2 takes %6.4fs'%(ttt1-ttt0))

comm.barrier()
if rank == 0:
    sys.exit()