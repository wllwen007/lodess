#! /usr/bin/python3
from __future__ import print_function
import sys
import select
import time
import datetime
import threading
import argparse
import multiprocessing as mp
import subprocess
import numpy as np
import pyrap.tables as pt
import os
from regions import RectangleSkyRegion
import losoto.h5parm as h5parm
from astropy.coordinates import SkyCoord
import astropy.units as u
import glob
import bdsf


'''
    Input: *msdemix files
           cal_solutions.h5
           skymodel, with the name of the skymodel prepended to it (e.g. 3C380-alsjfoieaj.skymodel)
    Output: stuff
            fitsfiles
'''

freqstep = 1

c3c380= np.array([-1.44194739, 0.85078014])
c3c196= np.array([2.15374139,0.8415521])

ROOT_FOLDER = '/net/rijn/data2/groeneveld/LoDeSS_files/'
HELPER_SCRIPTS = '/net/rijn/data2/groeneveld/LoDeSS_files/lofar_facet_selfcal/'
FACET_PIPELINE = ROOT_FOLDER + 'lofar_facet_selfcal/facetselfcal.py'
H5_HELPER = '/net/rijn/data2/groeneveld/lofar_helpers/'

def run_cmd(s,proceed=False,dryrun=False,log=None,quiet=False):
    '''modified from ddf-pipeline
    https://github.com/mhardcastle/ddf-pipeline/blob/master/utils/auxcodes.py
    '''
    print('Running: '+s)
    if not dryrun:
        if log is None:
            retval=subprocess.call(s,shell=True)
        else:
            retval=run_log(s,log,quiet)
        if not(proceed) and retval!=0:
           raise RuntimeError('FAILED to run '+s+': return value is '+str(retval))
        return retval
    else:
        print('Dry run, skipping this step')

def run_log(cmd,logfile,quiet=False):
    '''taken from ddf-pipeline
    https://github.com/mhardcastle/ddf-pipeline/blob/master/utils/pipeline_logging.py
    '''
    logfile = open(logfile, 'w')
    logfile.write('Running process with command: '+cmd+'\n')
    proc=subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,universal_newlines=True)
    while True:
        try:
            select.select([proc.stdout],[],[proc.stdout])
        except select.error:
            pass
        line=proc.stdout.readline()
        if line=='':
            break
        if not quiet:
            sys.stdout.write(line)
        ts='{:%Y-%m-%d %H:%M:%S}'.format(datetime.datetime.now())
        logfile.write(ts+': '+line)
        logfile.flush()
    retval=proc.wait()
    logfile.write('Process terminated with return value %i\n' % retval)
    return retval


def add_dummyms(msfiles):
    '''
    Add dummy ms to create a regular freuqency grid when doing a concat with DPPP
    '''
    if len(msfiles) == 1:
      return msfiles
    keyname = 'REF_FREQUENCY'
    freqaxis = []
    newmslist  = []

    # Check for wrong REF_FREQUENCY which happens after a DPPP split in frequency
    for ms in msfiles:        
        t = pt.table(ms + '/SPECTRAL_WINDOW', readonly=True)
        freq = t.getcol('REF_FREQUENCY')[0]
        t.close()
        freqaxis.append(freq)
    freqaxis = np.sort( np.array(freqaxis))
    minfreqspacing = np.min(np.diff(freqaxis))
    if minfreqspacing == 0.0:
       keyname = 'CHAN_FREQ' 
    
    
    freqaxis = [] 
    for ms in msfiles:        
        t = pt.table(ms + '/SPECTRAL_WINDOW', readonly=True)
        if keyname == 'CHAN_FREQ':
          freq = t.getcol(keyname)[0][0]
        else:
          freq = t.getcol(keyname)[0]  
        t.close()
        freqaxis.append(freq)
    
    # put everything in order of increasing frequency
    freqaxis = np.array(freqaxis)
    idx = np.argsort(freqaxis)
    
    freqaxis = freqaxis[np.array(tuple(idx))]
    sortedmslist = list( msfiles[i] for i in idx )
    freqspacing = np.diff(freqaxis)
    minfreqspacing = np.min(np.diff(freqaxis))
 
    # insert dummies in the ms list if needed
    count = 0
    newmslist.append(sortedmslist[0]) # always start with the first ms the list
    for msnumber, ms in enumerate(sortedmslist[1::]): 
      if int(round(freqspacing[msnumber]/minfreqspacing)) > 1:
        ndummy = int(round(freqspacing[msnumber]/minfreqspacing)) - 1
 
        for dummy in range(ndummy):
          newmslist.append('dummy' + str(count) + '.ms')
          print('Added dummy:', 'dummy' + str(count) + '.ms') 
          count = count + 1
      newmslist.append(ms)
       
    print('Updated ms list with dummies inserted to create a regular frequency grid')
    print(newmslist) 
    return newmslist

def _run_sing(runname):
    cmd = f'python launch_run.py {runname}'
    print(cmd)
    run_cmd(cmd)

def find_skymodel():
    measurementset = glob.glob('*msdemix')[2]
    tab = pt.table(measurementset+'::FIELD')
    adir = tab.getcol('DELAY_DIR')[0][0][:]
    tab.close()

    cdatta = SkyCoord(adir[0]*u.deg, adir[0]*u.deg, frame='icrs')

    targetsource = None

    if (cdatta.separation( SkyCoord(c3c380[0]*u.deg, c3c380[0]*u.deg, frame='icrs'))) < 0.2*u.deg:
        targetsource = '3c380'    
    if (cdatta.separation( SkyCoord(c3c196[0]*u.deg, c3c196[0]*u.deg, frame='icrs'))) <  0.2*u.deg:
        targetsource = '3c196' 
    return targetsource

def find_missing_stations():
    measurementset = glob.glob('*msdemix')[0]
    h5 = 'Band_PA.h5'

    # First look at the h5

    H5 = h5parm.h5parm(h5)
    a = H5.getSolset('calibrator')
    antlist_h5 = a.getSoltab('bandpass').getValues()[1]['ant']
    H5.close()

    # Now look at ms

    tab = pt.table(measurementset+'::ANTENNA')
    antlist_ms = tab.getcol('NAME')

    # compare them

    missinglist = []
    for ant in antlist_ms:
        if ant not in antlist_h5:
            missinglist.append(ant)
    return missinglist

def generate_boxfile(direction):
    width = 8*512*u.arcsec
    height = width
    parsed_dir = direction[1:-1].replace(',','+')
    coord = SkyCoord(parsed_dir,unit=(u.deg,u.deg))
    region = RectangleSkyRegion(center=coord,width=width,height = height)
    region.write('boxfile.reg',format='ds9')
    
def find_rms(thres = 0.002):
    '''
        Run this in the DD_cal directory. Iterates through all the run_X folders,
        finds the measurement sets and computes the snr
    '''
    import runwsclean as runw
    msses = glob.glob('run*/direction*/Dir*ms')
    msfullnames = [ms.split('/')[-1] for ms in msses]
    if len(msfullnames[0].split('.')) == 4:
        # Multiple MS per facet
        msnames = ['.'.join(ms.split('.')[:2]) for ms in msfullnames]
        msnums = [float(ms.split('Dir')[-1]) for ms in msnames]
        mstextnums = [ms.split('Dir')[-1] for ms in msnames] # Important for conserving the "0" case (or "10" case, for that matter...)
        msfirstnums = [int(str(num).split('.')[0]) for num in msnums]
    else:
        msnames = [ms.split('/')[-1].split('.')[0] for ms in msses]
        msnums = [int(ms.split('Dir')[-1]) for ms in msnames]
        mstextnums = msnums # Doesn't matter if there is only one measurement set...
        msfirstnums = msnums  # Doesn't matter if there is only one measurement set...

    sorting = np.argsort(msnums)

    msses = np.array(msses)[sorting]
    msnames = np.array(msnames)[sorting]
    msnums = np.array(msnums)[sorting]
    msfirstnums = np.array(msfirstnums)[sorting]
    mstextnums = np.array(mstextnums)[sorting]

    snrs = []
    thresholds = [] 
    for ms,msname in zip(msses,msnames):
        noise,flux,_,__ = runw.getmsmodelinfo(ms,'MODEL_DATA',fastrms=True)
        try:
            noise,flux,_,__ = runw.getmsmodelinfo(ms,'MODEL_DATA',fastrms=True)
            snr = flux/noise
            snrs.append(snr)
        except:
            snrs.append(np.nan)
        t = pt.table(ms).getcol('TIME')
        duration = t[-1] - t[0]
        thresholds.append(thres* np.sqrt(duration)/np.sqrt(18000)) # SNR scales naturally as sqrt(t), and we use 0.002 as reference
    
    snrs = np.array(snrs)
    toreject = np.where(snrs < thresholds)[0]
    
    os.chdir('RESULTS')
    os.mkdir('rejected')
    for torej in toreject:
        firstnum = msfirstnums[torej]
        where_firstnum = np.where(msfirstnums==firstnum)[0]
        toremove = mstextnums[where_firstnum]
        num = msnums[torej]
        print('Removing: ')
        print(', '.join(toremove))
        print('__________________________')

        for torem in toremove:
            os.system(f'mv h5files/direction{torem}.h5 rejected/direction{torem}.h5')
    # TODO: make sure that BOTH h5s are deleted if only one is deleted
    os.chdir('../')

def run(comb):
    '''
        Processes demixed datasets and outputs a 'raw' dataset, where only bandpass+polaligns
        are applied
    '''
    ms,missinglist = comb[0],comb[1]
    # First filter out missing stations
    if len(missinglist) > 0:
        msout = ms.split('.msdemix')[0] + '.split.ms'
        cmd =  'DPPP numthreads=80 msin=' + ms + ' msout.storagemanager=dysco '
        cmd += 'msout='+msout + ' '
        cmd += 'msout.writefullresflag=False '
        cmd += 'steps=[f2] '

        cmd += 'f2.type=filter f2.remove=True f2.baseline="'
        for missing in missinglist:
            cmd += f'!{missing}&&*;'
        cmd = cmd.rstrip(';')
        cmd += '"'
        print(cmd)
        subprocess.call(cmd, shell=True)
        msin = ms.split('.msdemix')[0] + '.split.ms'
    else:
        msin = ms
    msout = ms.split('.msdemix')[0] + '.corr.ms'
    msout = msout.split('archive/')[-1]
    cmd =  'DPPP numthreads=80 msin=' + msin + ' msout.storagemanager=dysco '
    cmd += 'msout='+msout + ' '
    cmd += 'msout.writefullresflag=False '
    cmd += 'steps=[applyPA,applyBandpass,applyBeam,avg] '

    cmd += 'applyPA.type=applycal applyPA.correction=polalign '
    cmd += 'applyPA.parmdb=Band_PA.h5 '

    cmd += 'applyBandpass.type=applycal applyBandpass.correction=bandpass '
    cmd += 'applyBandpass.parmdb=Band_PA.h5 '
    cmd += 'applyBandpass.updateweights=True '

    cmd += 'applyBeam.type=applybeam '
    cmd += 'applyBeam.updateweights=True '
    cmd += 'applyBeam.usechannelfreq=False ' # because we work on a single SB

    cmd += f'avg.type=averager avg.freqstep={freqstep} '

    print(cmd)
    subprocess.call(cmd, shell=True)

def initrun(LnumLoc):
    # Fixed for multiple sources

    if len(LnumLoc)==1:
        Lnum = LnumLoc[0].split('/')[-2]
    else:
        lnums = [l.split('/')[-2] for l in LnumLoc]
        fl = glob.glob(LnumLoc[0]+'/*msdemix')[0] # find example file
        t = pt.table(fl+'::FIELD')
        Lnum = t.getcol('CODE')[0]
    os.mkdir(Lnum)
    run_cmd(f'cp -r /net/rijn/data2/groeneveld/largefiles/Band_PA.h5 {Lnum}')
    os.chdir(Lnum)
    for loc in LnumLoc:
        if loc[0] == '/': #Absolute path
            tocopy = glob.glob(loc + '*msdemix')
        else:
            tocopy = glob.glob('../'+loc+'*msdemix')
        for cop in sorted(tocopy):
            print(f'Copying SB{cop.split("SB")[1][:3]}')
            run_cmd(f'cp -r {cop} .')

    target_source = find_skymodel()
    if target_source == '3c196':
        run_cmd(f'cp -r /net/bovenrijn/data1/groeneveld/software/prefactor/skymodels/3C196-pandey.skymodel .')
    elif target_source == '3c380':
        run_cmd(f'cp -r /net/bovenrijn/data1/groeneveld/software/prefactor/skymodels/3C380_8h_SH.skymodel 3C380-SH.skymodel')
    print(target_source)

def extract_directions(calibrator):
    filename = glob.glob('*MFS-image.fits')[0]
    # img = bdsf.process_image(filename, rms_box = (640,160), rms_map=True, thresh='hard', thresh_isl=10.0, thresh_pix=25.0)
    # img.write_catalog(outfile='regions_wsclean1.fits', bbs_patches='single', catalog_type='srl', clobber=True, format='fits')

    cmd = f'''python extract.py'''
    print(cmd)
    run_cmd(cmd)

def _run_demix(location):
    if location[-1] != '/':
        location += '/'
    run_cmd(f'python3 {location}averageandemix.py {location}')

def pre_init(location):
    ncpu = 4 # Be patient...
    run_cmd(f'cp -r {ROOT_FOLDER}prerun/*py {location}')
    run_cmd(f'cp -r {ROOT_FOLDER}prerun/demix.sourcedb {location}')
    demix_pool = []

    for i in range(ncpu):
        proc = mp.Process(target=_run_demix, args=(location,))
        proc.start()
        demix_pool.append(proc)
        time.sleep(10*60) # Wait 10 minutes for the SB to load, also buffers for optimal performance
    
    for proc in demix_pool:
        proc.join()

def generate_ddf_bashfile(msname):
    mslist = glob.glob('*ms')
    if len(mslist) > 1:
        # Multiple measurements
        msname = ','.join(glob.glob('*ms'))
    base_cmd = f'''export NUMEXPR_MAX_THREADS=96
echo $NUMEXPR_MAX_THREADS
DDF.py --Data-ChunkHours=0.5 --Debug-Pdb=never --Parallel-NCPU=32 --Cache-Dir ./ --Data-MS {msname} --Data-ColName DATA --Data-Sort 1 --Output-Mode Clean --Deconv-CycleFactor 0 --Deconv-MaxMinorIter 1000000 --Deconv-RMSFactor 2.0 --Deconv-FluxThreshold 0.0 --Deconv-Mode HMP --HMP-AllowResidIncrease 1.0 --Weight-Robust -0.5 --Image-NPix 8192 --CF-wmax 50000 --CF-Nw 100 --Beam-CenterNorm 1 --Beam-Smooth 1 --Beam-Model LOFAR --Beam-LOFARBeamMode A --Beam-NBand 1 --Beam-DtBeamMin 5 --Output-Also onNeds --Image-Cell 8.0 --Freq-NDegridBand 7 --Freq-NBand 7 --Mask-Auto 1 --Mask-SigTh 2.0 --GAClean-MinSizeInit 10 --GAClean-MaxMinorIterInitHMP 100000 --Facets-DiamMax 1.5 --Facets-DiamMin 0.1 --Weight-ColName WEIGHT_SPECTRUM --Output-Name run1 --DDESolutions-DDModeGrid AP --DDESolutions-DDModeDeGrid AP --RIME-ForwardMode BDA-degrid --Output-RestoringBeam 45.0 --DDESolutions-DDSols merged.h5:sol000/phase000+amplitude000 --Deconv-MaxMajorIter 8 --Deconv-PeakFactor 0.005 --Cache-Reset 1 --Misc-IgnoreDeprecationMarking=1 #>> ddfacet-c0.log 2>&'''
    if len(mslist) > 1:
        base_cmd = base_cmd.replace('merged.','merged.*.')
    with open('cmd1.sh','w') as handle:
        handle.write(base_cmd)
    
    second_cmd = f'''export NUMEXPR_MAX_THREADS=96
echo $NUMEXPR_MAX_THREADS
DDF.py --Data-ChunkHours=0.5 --Debug-Pdb=never --Parallel-NCPU=32 --Cache-Dir ./ --Mask-External=run1mask.fits --Predict-InitDicoModel=run1.01.DicoModel --Data-MS {msname} --Data-ColName DATA --Data-Sort 1 --Output-Mode Clean --Deconv-CycleFactor 0 --Deconv-MaxMinorIter 1000000 --Deconv-RMSFactor 2.0 --Deconv-FluxThreshold 0.0 --Deconv-Mode HMP --HMP-AllowResidIncrease 1.0 --Weight-Robust -0.5 --Image-NPix 8192 --CF-wmax 50000 --CF-Nw 100 --Beam-CenterNorm 1 --Beam-Smooth 1 --Beam-Model LOFAR --Beam-LOFARBeamMode A --Beam-NBand 1 --Beam-DtBeamMin 5 --Output-Also onNeds --Image-Cell 8.0 --Freq-NDegridBand 7 --Freq-NBand 7 --Mask-Auto 1 --Mask-SigTh 2.0 --GAClean-MinSizeInit 10 --GAClean-MaxMinorIterInitHMP 100000 --Facets-DiamMax 1.5 --Facets-DiamMin 0.1 --Weight-ColName WEIGHT_SPECTRUM --Output-Name run2 --DDESolutions-DDModeGrid AP --DDESolutions-DDModeDeGrid AP --RIME-ForwardMode BDA-degrid --Output-RestoringBeam 45.0 --DDESolutions-DDSols merged.h5:sol000/phase000+amplitude000 --Deconv-MaxMajorIter 8 --Deconv-PeakFactor 0.005 --Cache-Reset 1 --Misc-IgnoreDeprecationMarking=1 #>> ddfacet-c1.log 2>&'''
    if len(mslist) > 1:
        second_cmd = second_cmd.replace('merged.','merged.*.')
    with open('cmd2.sh','w') as handle:
        handle.write(second_cmd)

    
def calibrator(flagstation=None,nthreads=6):
    missinglist = find_missing_stations()

    mslist = sorted(glob.glob('*msdemix'))
    comblist = [(ms,missinglist) for ms in mslist]
    pl = mp.Pool(nthreads)
    pl.map(run,comblist)

    msnames = glob.glob('*corr*')
    msname = msnames[2].split('SB')[0]
    corrected_msnames = add_dummyms(msnames)
    outname = msname + 'concat.ms'
    retstr = '['
    for j in corrected_msnames:
        retstr += f'{j},'
    retstr = retstr[:-1] + ']'
    cmd = f'DPPP numthreads=80 msin={retstr} msout={outname} msout.storagemanager=dysco msout.writefullresflag=false msin.missingdata=true msin.orderms=false steps=[]'
    print(cmd)
    run_cmd(cmd)

    input_concat = glob.glob('*concat.ms')[0]
    skymodel = glob.glob('*skymodel')[0]
    sourcename = skymodel.split('-')[0]
    if sourcename == '3C380':
        # I am so sorry for this line
        sourcename = '3c380'

    if flagstation != None:
        cmd = f'DPPP msin={outname} msout=. steps=[preflagger] preflagger.baseline="{flagstation}&&*"'
        run_cmd(cmd)
    
    cmd = f'''python {FACET_PIPELINE} --helperscriptspath={HELPER_SCRIPTS} --helperscriptspathh5merge={H5_HELPER} --BLsmooth --ionfactor 0.02 --docircular --no-beamcor --skymodel={skymodel} --skymodelsource={sourcename} --soltype-list="['scalarphasediff','scalarphase','complexgain']" --solint-list="[4,1,8]" --nchan-list="[1,1,1]" --smoothnessconstraint-list="[0.6,0.3,1]" --imsize=4096 --uvmin=300 --stopafterskysolve --channelsout=24 --fitspectralpol=False --soltypecycles-list="[0,0,0]" --normamps=False --stop=1 --smoothnessreffrequency-list="[30.,20.,0.]" --doflagging=True --doflagslowphases=False --flagslowamprms=25 {input_concat}'''
    print(cmd)
    run_cmd(cmd,log='calibrator_facetselfcal.log')   

def individual_target(Lnum,calfile,target,nthreads=6):
    '''
        This runs the individual target part of the pipeline
        splits up the data in individual runs

        calfile: abs path to calfile
    '''
    os.mkdir(Lnum)
    run_cmd(f'mv {Lnum}*msdemix {Lnum}')
    os.chdir(Lnum)
    run_cmd(f'cp -r {calfile} calibrator.h5')
    run_cmd(f'cp -r ../Band_PA.h5 .')
    missinglist = find_missing_stations()

    mslist = sorted(glob.glob('*msdemix'))
    comblist = [(ms,missinglist) for ms in mslist]
    pl = mp.Pool(nthreads)
    pl.map(run,comblist)

    msnames = glob.glob('*corr*')
    msname = msnames[2].split('SB')[0]
    corrected_msnames = add_dummyms(msnames)
    outname = msname + 'concat.ms'
    retstr = '['
    for j in corrected_msnames:
        retstr += f'{j},'
    retstr = retstr[:-1] + ']'
    cmd = f'DPPP numthreads=80 msin={retstr} msout={outname} msout.storagemanager=dysco msin.missingdata=true msin.orderms=false msout.writefullresflag=false steps=[]'
    print(cmd)
    run_cmd(cmd)

    # Go to circular ...
    run_cmd(f'cp -r {ROOT_FOLDER}lin2circ.py .')
    run_cmd(f'python lin2circ.py -i {outname} -c DATA -o DATA_CIRC')

    # Now, apply the calfile

    cmd = f'DPPP msin={outname} msout=. steps=[ac1,ac2] msout.datacolumn=CALCORRECT_DATA_CIRC msin.datacolumn=DATA_CIRC '
    cmd += f'ac1.type=applycal ac1.parmdb=calibrator.h5 ac1.solset=sol000 ac1.correction=phase000 '
    cmd += f'ac2.type=applycal ac2.parmdb=calibrator.h5 ac2.solset=sol000 ac2.correction=amplitude000 '
    print(cmd)
    run_cmd(cmd)

    # ... and go back to linear

    run_cmd(f'python lin2circ.py -i {outname} -c CALCORRECT_DATA_CIRC -b -l CALCORRECT_DATA')
    run_cmd(f'cp -r {outname} ../')

    # Phaseshift to target+average

    cmd = f'DPPP msin={outname} msin.datacolumn=CALCORRECT_DATA msout=phaseshifted_{outname} msout.storagemanager=dysco steps=[phaseshift,averager] '
    cmd += f'phaseshift.phasecenter={target} averager.freqstep=4 msout.writefullresflag=false '
    print(cmd)
    run_cmd(cmd)

    run_cmd(f'cp -r phaseshifted_{outname} ../')
    os.chdir('../') # Move back to main folder

def consolidated_target(target):
    os.mkdir('target_cal')
    os.chdir('target_cal')
    run_cmd('mv ../phaseshifted_* .')
    generate_boxfile(target)
    # The following line uses a wildcard statement to glob all phaseshifted measurement sets
    cmd = f'''python {FACET_PIPELINE} --helperscriptspath {HELPER_SCRIPTS} --helperscriptspathh5merge={H5_HELPER} --pixelscale 8 -b boxfile.reg --antennaconstraint="['core',None]" --BLsmooth --ionfactor 0.02 --docircular --startfromtgss --soltype-list="['scalarphasediffFR','tecandphase']" --solint-list="[24,1]" --nchan-list="[1,1]" --smoothnessconstraint-list="[1.0,0.0]" --uvmin=300 --channelsout=24 --fitspectralpol=False --soltypecycles-list="[0,0]" --normamps=False --stop=5 --smoothnessreffrequency-list="[30.,0]" --doflagging=True --doflagslowphases=False --flagslowamprms=25 phaseshifted_*'''
    print(cmd)
    run_cmd(cmd, log='target_di_facetselfcal.log')   

    # Make a direction independent image of the whole field
    os.chdir('..')
    os.mkdir('DI_image')
    run_cmd('cp -r target_cal/merged_selfcalcyle004* .') # Keep their original names - we know what form they are in
    for outname in glob.glob('L*concat.ms'):
        cmd = f'DPPP msin={outname} msout=DI_image/corrected_{outname} msin.datacolumn=CALCORRECT_DATA_CIRC steps=[ac1,ac2] msout.writefullresflag=false msout.storagemanager=dysco '
        cmd += f'ac1.type=applycal ac1.parmdb=merged_selfcalcyle004_phaseshifted_{outname}.copy.h5 ac1.solset=sol000 ac1.correction=phase000 '
        cmd += f'ac2.type=applycal ac2.parmdb=merged_selfcalcyle004_phaseshifted_{outname}.copy.h5 ac2.solset=sol000 ac2.correction=amplitude000 '
        print(cmd)
        run_cmd(cmd)
    os.chdir('DI_image')
    
    wscleancmd = f'wsclean -no-update-model-required -minuv-l 80.0 -size 8192 8192 -reorder -parallel-deconvolution 2048 -weight briggs -0.5 -weighting-rank-filter 3 -clean-border 1 -parallel-reordering 4 -mgain 0.8 -fit-beam -data-column DATA -padding 1.4 -join-channels -channels-out 8 -auto-mask 2.5 -auto-threshold 0.5 -pol i -baseline-averaging 2.396844981071314 -use-wgridder -name image_000 -scale 8.0arcsec -niter 150000 corrected_*'
    print(wscleancmd)
    run_cmd(wscleancmd,log='target_di_image.log')
    os.chdir('..')

    # Make a guesstimate of the regions
    os.mkdir('extract_directions')
    os.chdir('extract_directions')
    run_cmd(f'cp -r ../DI_image/image_000-MFS-image.fits .')
    run_cmd(f'cp -r {ROOT_FOLDER}DI/extract.py .')
    run_cmd(f'cp -r {ROOT_FOLDER}DI/split_rectangles.py .')
    extract_directions(target)
    run_cmd(f'python split_rectangles.py regions_ws1.reg')

def target(calfiles,target,nthreads):
    '''
        DI Target pipeline V2.0
        Works for multiple runs of the same pointing
    '''
    # First, check which L numbers you have
    msdemix_folders = glob.glob('*msdemix')
    Lnums = [ms.split('_')[0] for ms in msdemix_folders]
    Lnums_unique = np.unique(Lnums)
    if len(Lnums_unique)!=len(calfiles):
        raise RuntimeError("There is a mismatched between the files in the main folder and the calibrators you have supplied. Maybe something went wrong in the initialization phase?")
    
    # Run the individual pipeline for each L number separately
    for Lnum,calfile in zip(Lnums_unique,calfiles):
        individual_target(Lnum,calfile,target,nthreads)
    
    # Run the consolidated pipeline for all files
    consolidated_target(target)

def dd_pipeline(location,boxes,nthreads,target):
    '''
        This pipeline requires boxes to be pre-determined, as this is 
        a difficult step to automize. Maybe in the future...
    '''
    if boxes == None:
        boxes = f'{location[0]}/extract_directions/regions_ws1/'
    boxes = os.path.abspath(boxes)
    os.chdir(location[0]) # For now... but not really. This should be pointing to the name of the pointing
    os.mkdir('DD_cal')
    os.chdir('DD_cal')
    run_cmd(f'cp -r {boxes} ./rectangles')
    run_cmd(f'cp -r {ROOT_FOLDER}DD/* .')
    run_cmd(f'cp -r ../DI_image/image_000-????-model.fits .')
    run_cmd(f'cp -r ../DI_image/*ms .')

    # See if any boxes are present, else raise an error
    boxes_present = len(glob.glob('rectangles/*'))
    if boxes_present < 1:
        raise RuntimeError("No boxes are found. Are you sure ran the DI pipeline first - and if so, are you sure that it created any regions? Do that by hand, if necessary")

    spawn_delay = 3600 # == 1 hr
    threadlist = []
    for ii in range(nthreads):
        t = threading.Thread(target=_run_sing,args=str(ii))
        t.daemon = True
        t.start()
        threadlist.append(t)
        time.sleep(spawn_delay)
    for t in threadlist:
        t.join()
    # Go back to the root directory
    os.chdir('../../')


def DDF_pipeline(location,direction):
    '''
        This pipeline starts off where the DD pipeline stops:
        it checks what the noise is for 

        make sure it rejects both h5s if one of the h5s is bad
        Also, make sure it gives two merged h5 files...
    '''
    run_cmd(f'cp -r {FACET_PIPELINE} runwsclean.py')
    os.chdir(location[0]) # Again, this should be the pointing name...
    if not os.path.isdir('DD_cal'):
        print("You need to perform DD calibration before running the facet-imaging pipeline. Also make sure that you are giving it the directory of the pointing (not the MS)")
        return 1
    else:
        pass
    os.chdir('DD_cal')
    run_cmd('python extract_results.py')
    find_rms()

    if len(glob.glob('RESULTS/h5files/direction*h5')[0].split('.')) == 3:
        # multi ms
        # do a merge run for each MS number
        h5list = glob.glob('RESULTS/h5files/direction*h5')
        nums = [h5.split('.')[1] for h5 in h5list]
        n_max = np.max(np.array(nums,dtype=int))
        for n in range(n_max + 1):
            cmd = f'python h5_merger.py -out merged.{n}.h5 -in RESULTS/h5files/*.{n}.h5 --ms run_0/direction0/Dir0.{n}.peel.ms '
            if direction != None:
                crdlist = direction.lstrip('[').rstrip(']').split(',')
                crdlist = [crd.split('deg')[0] for crd in crdlist]
                crd = SkyCoord(*crdlist,unit = (u.deg,u.deg))
                radiancoord = str([crd.ra.to(u.radian).value,crd.dec.to(u.radian).value]).replace(' ','')
                cmd += f'--add_direction {radiancoord}'
            print(cmd)
            run_cmd(cmd)

        pass
    else:
        # single ms
        cmd = f'python h5_merger.py -out merged.h5 -in RESULTS/h5files/* --ms run_0/direction0/Dir0.peel.ms '
        if direction != None:
            crdlist = direction.lstrip('[').rstrip(']').split(',')
            crdlist = [crd.split('deg')[0] for crd in crdlist]
            crd = SkyCoord(*crdlist,unit = (u.deg,u.deg))
            radiancoord = str([crd.ra.to(u.radian).value,crd.dec.to(u.radian).value]).replace(' ','')
            cmd += f'--add_direction {radiancoord}'
        print(cmd)
        run_cmd(cmd)

    # now make facet imaging folder and generate the shell files
    msname = glob.glob('*ms')[0]
    os.chdir('../')
    os.mkdir('facet_imaging')
    os.chdir('facet_imaging')
    run_cmd(f'cp -r {ROOT_FOLDER}/DDF/make_mask.py .')
    run_cmd(f'cp -r ../DD_cal/merged.*h5 .')
    run_cmd(f'cp -r ../DD_cal/*ms ./')
    generate_ddf_bashfile(msname)
    
    # And now run the two DDF runs
    run_cmd('bash cmd1.sh',log='ddf_image1.log')
    # run_cmd('MakeMask.py --RestoredIm run1.int.restored.fits --Th 3')
    # Run on the apparent image (flat noise)
    run_cmd('python make_mask.py -s run1.app.restored.fits -m run1mask.fits')
    run_cmd('bash cmd2.sh',log='ddf_image2.log')


if __name__ == "__main__":
    parse = argparse.ArgumentParser(description='LoDeSS calibrator+target pipeline')
    parse.add_argument('location',help='Location of the downloaded+demixed data. For now, it is important that the final folder begins with L??????.',type=str,nargs='+')
    parse.add_argument('--cal_H5',help='H5 file from the calibrator source. This is used to make an initial correction', default=None,nargs='*')
    parse.add_argument('--direction',help='Direction to go to when using the target pipeline. Format: "[xxx.xxdeg,yyy.yydeg]"', default=None,type=str)
    parse.add_argument('--boxes', help='Folder with boxes, called DirXX. Needed for direction dependent calibration')
    parse.add_argument('--nthreads', default=6, help='Amount of threads to be spawned by DD calibration. 5 will basically fill up a 96 core node (~100 load avg)')
    parse.add_argument('--demix', '--prerun',action = 'store_true', help='Do this if the folder contains raw .tar files instead of demixed folders. Untarring has to happen on the node itself - so from a performance POV this might not be a good choice.')
    parse.add_argument('--delete_files', action='store_true', help='Deletes files after running the pipelne. Only recommended for the calibrator pipeline!')
    parse.add_argument('--pipeline', help='Pipeline of choice', choices=['DD','DI_target','DI_calibrator','DDF','full'])
    parse.add_argument('--flag_station', help='Flags these stations, particularly handy for the calibrator pipeline', default=None)
    parse.add_argument('-d','--debug', help='Debugging option, please don\'t touch',action='store_true')

    res = parse.parse_args()

    if res.cal_H5:
    # Check here if the input is valid
        if len(res.cal_H5)!=len(res.location) and res.pipeline=='DI_target':
            raise ValueError('Must give as many calibrator files as MS locations when running the DI pipeline')
        calexists = np.array([os.path.isfile(cf) for cf in res.cal_H5])
        if np.any(~calexists):
            for ci,cf in enumerate(res.cal_H5):
                if not calexists[ci]: print('calibrator file',cf,'is missing')
            raise RuntimeError('Specified calibrator files missing')
        
    if res.delete_files and res.pipeline!='DI_calibrator':
        raise ValueError('Deleting files automatically is currently only supported for the DI calibrator pipeline.')

    location = res.location
    call = ' '.join(sys.argv).replace('(','"(').replace(')',')"')
    if not os.path.isfile('calls.log'):
        os.system('touch calls.log')
    with open('calls.log','a') as handle:
        handle.write('\n')
        handle.write('python '+call)

    if res.direction != None:
        if res.direction[0] == '(':
            # Modify the direction string so it is a bit easier to use
            # convert it to the "normal way"
            resstring = res.direction
            reslist = resstring.replace('(','').replace(')','').split(', ')
            reslist = [i+'deg' for i in reslist]
            new_restring = '[' + ','.join(reslist) + ']'
            res.direction = new_restring
            print('Reformatting direction to: '+res.direction)

    if res.debug:
        for a in vars(res):
            print(a,vars(res)[a])
        print("Stopping for debugging...")
        sys.exit(0)

    if res.demix:
        for loc in location:
            pre_init(loc)

    if res.pipeline=='DI_calibrator':
        for loc in location:
            initrun(location)
            calibrator(res.flag_station,res.nthreads)
            lastwd = os.path.abspath(os.getcwd())
            os.chdir('..')
        print('----------------')
        print(lastwd)
    elif res.pipeline=='DD':
        # This step doesn't necessarily need a target
        dd_pipeline(location,res.boxes,res.nthreads,res.direction)
    elif res.pipeline=='DI_target':
        # This step absolutely needs a target
        calfiles_abs = [os.path.abspath(calfile) for calfile in res.cal_H5]
        initrun(location)
        target(calfiles_abs,res.direction,res.nthreads)
    elif res.pipeline=='DDF':
        DDF_pipeline(location,res.direction)
    elif res.pipeline=='full':
        # Run the full pipeline.
        # This is useful BUT PLEASE CHECK
        # EACH INDIVIDUAL STEP
        #
        # PLEASE DO IT
        # Also note the two chdirs necessary for running this code properly
        calfiles_abs = [os.path.abspath(calfile) for calfile in res.cal_H5]
        initrun(location)
        target(calfiles_abs,res.direction,res.nthreads)
        os.chdir("../") # Go back from extract_directions to main root
        wd = os.getcwd()
        dd_pipeline('./','./extract_directions/regions_ws1/',res.nthreads,None)
        os.chdir(wd)
        DDF_pipeline('./',None)

    if res.delete_files and res.pipeline == 'DI_calibrator':
        # Delete measurement sets. This should be the bulk anyways...
        os.chdir(lastwd)
        os.system('rm -rf *msdemix')
        os.system('rm -rf *.split.ms')
        os.system('rm -rf *corr.ms')
        os.mkdir('FITSimages')
        os.system('mv *fits FITSimages')
