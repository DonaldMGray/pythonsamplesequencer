import time
import datetime
import threading
import mido
import pygame
import glob
import numpy
import os
import sys
import re
import logging
import signal
import argparse
import json
import pickle
import copy  #deep object copy
import lcdDisplay  #dmg - display module control
#from collections import namedtuple
from enum import Enum, auto

#set logging level
#LOG_LEVEL = logging.DEBUG
LOG_LEVEL = logging.INFO
PRINT_TIME = False

#may help pygame??
os.environ["SDL_VIDEODRIVER"] = "dummy"

#The following are global for easy access:
#  seqMgr, sampMgr, display

"""
This is a drum sequence generator.

A 'sequence' consists of recorded notes in time.  A sequence has a length numMeasures * numBeats * numSubBeats
Sequences can be stored and reloaded (to local mem).  Can also be saved to file and loaded in via cmd line arg
The “current Sequence” is the one currently being edited.
The starting current Sequence is empty


"Samples" allow mapping from notes to music samples.
The “current sample set” is the one being used by the current sequence at any one time.
The starting current Sample is the first sub-directory alphabetically in the Samples directory.
By default, samples are “unbound” to sequences, meaning the current sample can be changed and the current sequence will then play using the (new) current sample.
TODO -If a sequence is “sample bound”, then the samples associated with the sequence notes are remembered and will not change when the current sample is changed.

Major classes:
* Sequence manager –loads samples; plays sequences; listens for user events
    Note that samples should be 16bps (or less?)

Notes:
    Polyphony - The mixer has n channels (8?)
    For each channel, only one sound can play at a time.  New sound interrupts 

Mixer channels (8 total?):
    Polyphony:          chans [0:5] (6)
    User played note:   chan 6
    Metronome:          chan 7
TODO
--

"""

class PygameSetup():
    """
    Handle pygame initialization
    """
    def initPygame(self):
        # buffer size should be set low to ensure good timing; but too low can cause audio glitches
        # ~512 rcmd
        pygame.mixer.pre_init(frequency=22050, size=-16, channels=8, buffer=512)
        #pygame.init()  #don't init all of pygame as this gets us into issues with the display
        pygame.mixer.init()
        #keyboard event handling - only looking for keyboard up/down (not mouse)
        pygame.display.set_mode()
        pygame.event.set_allowed(None)
        pygame.event.set_allowed(pygame.KEYDOWN)
        pygame.event.set_allowed(pygame.KEYUP)

#needs to be set for the specific Midi input device
midiNoteList = [36,37,38,39, 40,41,42,43, 44,45,46,47, 48,49,50,51]
midiDevice = 1  #The nanopad tends to be device #1

class ThreadedMidi(threading.Thread):
    """
    Handle Midi messages - using "mido" library
    """
    def __init__(self, *args, **kwargs):
        threading.Thread.__init__(self, *args, **kwargs)
        self.daemon = True  #die if parant process dies
        self.start()
    def run(self):
        # get mido going
        midiPorts = mido.get_input_names()
        logging.debug("Midi ports: %s", midiPorts)
        if len(midiPorts) == 1:
            logging.warning("MIDI DEVICE NOT CONNECTED")
            exit()
        inport = mido.open_input(midiPorts[midiDevice])
        for msg in inport:
            #logging.debug("Midi msg==> %s", msg)
            if msg.type == "program_change":
                toggleSeq()
            elif msg.type == "note_on":
                seqMgr.handleNoteIn(msg.note)

class SeqTime():
    """
    Holds time signature elements - beatsPerMinute, numBeats, divisions per beat

    "isTock" is introduced to double the sampling rate (2x faster than the subbeat rate)
    This is important for capturing input - we want 2x sampling so we can "round" the capture time to be 
    closest to the right time point
    """
    def __init__(self, seq):
        self.seq = seq
        self.timeSig = seq.sequence['timeSig']
        self.clearTime()

    def clearTime(self):
        """ reset time clock """
        self.measure = self.timeSig['numMeasures']-1
        self.beat = self.timeSig['numBeats']-1
        self.subBeat = self.timeSig['numSubBeats']-1
        self.isTock = True #isTock is true for the second half of the interval

    def advanceTime(self):
        self.isTock = not self.isTock
        if not self.isTock:
            self.subBeat = (self.subBeat + 1) % self.timeSig['numSubBeats']
            if self.subBeat == 0:
                self.beat = (self.beat + 1) % self.timeSig['numBeats']
                if self.beat == 0:
                    self.measure = (self.measure + 1) % self.timeSig['numMeasures']

    def printTime(self):
        print(self.measure+1, "-", self.beat+1, ":", self.subBeat+1, "  T:", self.tick)
        #logging.info('%s-%s:%s T:%s', self.measure+1, self.beat+1, self.subBeat+1, self.tick)

    @property
    def tick(self):
       return (  self.measure*self.timeSig['numBeats']*self.timeSig['numSubBeats']
                + self.beat*self.timeSig['numSubBeats']
                + self.subBeat)

    @property
    def roundedTick(self):
        #handles timing so input lines up properly to note time points
        if not self.isTock:
            return self.tick
        else:
            return (self.tick + 1) % self.seq.numTicks


DEFAULT_BPM = 120
DEFAULT_NUMMEAS = 4
DEFAULT_NUMBEATSPERMEAS = 4
DEFAULT_NUMSUBBEATS = 2
numPoly = 6  #how many simultaneous voices
class Sequence():
    """ 
    A polyphonic sequence - [ONLY ONE VOICE PER TICK SUPPORTED CURRENTLY!!]
    Polyphonic uses the multiple channels of the mixer (8 total, one used by metronome)
    Note however that if two notes happen to be using the same channel and the first note is a long duration, it will get interrupted by the next note on that same channel
    """

    def __init__(self, arg):
        if isinstance(arg, dict):   #must be dictionary of timeSig
            self.timeSig = self.TimeSig(arg).timeSig
            self.sequence = self.newSequence(self.timeSig)
            self._addedNotes = list()  #list of ticks where notes have been added to sequence (allows deleting in LIFO order)
        elif isinstance(arg, str):  #must be a filename to load
            with open (arg, mode="r") as jsonFile:
                seq=json.load(jsonFile)
            self.timeSig = seq['timeSig']
            self.sequence = self.newSequence(self.timeSig)
            self.sequence['noteList']=seq['noteList']
            self._addedNotes = list()  #list of ticks where notes have been added to sequence (allows deleting in LIFO order)
            print ("loaded sequence from: {0}\n {1} {2}".format(arg, seq['timeSig'], seq['noteList']) )
        else:
            raise TypeError('Type: {0} not supported'.format(type(arg)))

    class TimeSig:
        def __init__ (self, timeSigArgs):
            self.timeSig = dict()
            if timeSigArgs['numMeasures'] is not None:
                self.timeSig['numMeasures']=int(timeSigArgs['numMeasures'])
            else:
                self.timeSig['numMeasures']=DEFAULT_NUMMEAS
            if timeSigArgs['numBeats'] is not None:
                self.timeSig['numBeats']=int(timeSigArgs['numBeats'])
            else:
                self.timeSig['numBeats']=DEFAULT_NUMBEATSPERMEAS
            if timeSigArgs['numSubBeats'] is not None:
                self.timeSig['numSubBeats']=int(timeSigArgs['numSubBeats'])
            else:
                self.timeSig['numSubBeats']=DEFAULT_NUMSUBBEATS

    def newSequence(self, timeSig):
        seq = {
                "timeSig" : timeSig,
                "noteList" : [list() for x in range(self.numTicks)]
                }

        timeSig = seq['timeSig']

        return seq

    @property
    def numTicks(self):
        return self.timeSig['numMeasures'] * self.timeSig['numBeats'] * self.timeSig['numSubBeats']

    def clear(self):
        self.sequence['noteList'] = [list() for x in range(self.numTicks)]
        self._addedNotes = list()  #list of ticks where notes have been added to sequence (allows deleting in LIFO order)

    @property
    def seqModified(self):
        return len(self._addedNotes) != 0

    def addNote(self, tick, note):
        noteList = self.sequence['noteList']
        if len(noteList[tick]) >= numPoly:
            logging.debug("Overflowed poly on tick: %s", tick)
            del noteList[tick][0]
        self._addedNotes.append(tick)
        noteList[tick].append(note) 
        logging.debug('added: %s at: %s', note, tick)

    def delNote(self):
        #delete last note (remember to delete from _addedNotes)
        if len(self._addedNotes) == 0: return #no added notes to delete
        noteTick = self._addedNotes.pop()
        self.sequence['noteList'][noteTick].pop()  #pop the last from _addedNotes; use that to index the tick, then pop from the voice list
        logging.debug('deleted from tick: %s', noteTick)

    def saveSequence(self): #save seq to file via json (restore is by creating Sequence(fileName) )
        fileName = "../savedSequences/sequence" + datetime.datetime.now().strftime("%Y_%m_%d__%H_%M") + ".json"
        logging.info("Saving sequence file: " + fileName)
        with open (fileName, mode="w") as jsonFile:
            json.dump(self.sequence, jsonFile, indent=4)
        os.chmod(fileName, 0o666)  #give write permission to all
        #print (json.dumps(self.sequence, indent=4) )    #print the string out



#Mixer channel allocation
#[0:5] are for 6 poly voices 
chanUserInput = 6   #for "live" note input
chanMetro = 7   #which mixer channel to use for metronome

class SequenceMgr():
    """
    Manages all sequences & samples
    Responds to user events from keypad (not midi pad)
    """
    #This class is effectively a singleton, so not sure the "self._myvar" usage is needed.  Good hygene?
    def __init__(self, timeArgDict):
        self._timer = None
        self.is_running = False
        self.metroOn = True
        if timeArgDict['bpm'] is not None:
            self.beatsPerMinute=int(timeArgDict['bpm'])
        else:
            self.beatsPerMinute=DEFAULT_BPM
        self.currSeq = Sequence(timeArgDict)
        self.currSeqNum = 0
        self.recording = False
        #self.seqList = [Sequence(timeArgDict)] * 10   #pre-init list of sequences in mem
        self.seqList = [None] * 10   #pre-init list of sequences in mem

    def start(self):
        """ sequence start/stop & callback handling """
        if not self.is_running:
            self.is_running = True
            self.next_call = time.time()
            #self.seqTime.clearTime() #restart the time sequence
        else:
            self.next_call += self.interval
        self._timer = threading.Timer(self.next_call - time.time(), self._run)
        self._timer.start()

    def stop(self):
        self._timer.cancel()
        self.is_running = False

    def _run(self):
        self.start() #set next timer trigger
        self.advanceSequence() #run the sequencer

    @property 
    def currSeq(self):
        return self._currSeq
    @currSeq.setter
    def currSeq(self, value):
        self._currSeq = value
        self.seqTime = SeqTime(self.currSeq)

    @property
    def beatsPerMinute(self):
        return self._beatsPerMinute
    @beatsPerMinute.setter
    def beatsPerMinute(self, value):
        if value < 10:
            value = 10
        self._beatsPerMinute = value

    @property
    def interval(self):
        """ time interval """
        return 60 / self.beatsPerMinute / self.seqTime.timeSig['numSubBeats'] / 2  #the 2 is because of "isTock"

    def updateDisplay(self):
        display.updateSettings(self.beatsPerMinute, sampMgr.currSampleDir, self.currSeqNum, self.recording)

    def advanceSequence(self):
        #bump the time - do this first so that everything is lined up to the new tick
        self.seqTime.advanceTime()
        #and the display
        display.updateTime(self.seqTime.measure+1, self.seqTime.beat+1)

        #play note(s) in sequence
        if self.seqTime.isTock == False:  #only play on the front half of the interval
            #handle metronome
            if self.seqTime.subBeat == 0: 
                if PRINT_TIME:
                    self.seqTime.printTime()  #note that we are only printing beats (not sub-beats)
                if self.metroOn:
                    if self.seqTime.beat == 0:
                        if self.seqTime.measure == 0: 
                            metroSamp = sampMgr.chime
                            metroVol = 1.0   #ideally use a chime sound here
                        else: 
                            metroSamp = sampMgr.metro
                            metroVol = 0.6
                    else:
                        metroSamp = sampMgr.metro
                        metroVol = 0.3
                    pygame.mixer.Channel(chanMetro).set_volume(metroVol)
                    pygame.mixer.Channel(chanMetro).play(metroSamp);

            #Now play all the notes in this tick
            seqNoteList = self.currSeq.sequence['noteList']
            try:
                seqTickNotes = seqNoteList[self.seqTime.tick]
                if len(seqTickNotes) != 0:
                    self.mixChan=0
                    if True:    #enable poly
                        for note in seqTickNotes:
                            self.playMidiNote(note, self.mixChan)
                            self.mixChan += 1
                    else:  #no poly
                        self.playMidiNote(seqTickNotes[0],0) 
            except IndexError:
                logging.warning('Tick count is longer than sequence note list')


    def handleNoteIn(self,note):
        """
        Handle midi note pressed
        If recording, add to the current sequence
        """
        if self.recording and self.seqTime.isTock:  #avoid the double play that happens if we enter a note in the Tock while recording (because the recording will play it the 2nd time)
            #can be a bit disconcerting with really slow tempo, but I think is better experience
            pass
        else:
            self.playMidiNote(note,chanUserInput)  #Poly allowed for chans [0:5], metronome is chan 7
        #add note to sequence
        if self.recording:
            noteTick = self.seqTime.roundedTick
            self.currSeq.addNote(noteTick, note)

    def playMidiNote(self,note,chan):
        """ Based on midi input msg, play a sound """
        try:
            sampNum = midiNoteList.index(note)
        except ValueError:
            #pass
            return
        sampleSet = sampMgr.sampleSets[sampMgr.currSampleDir]
        if len(sampleSet.sampleSounds) < sampNum+1:
            logging.debug('Sample %s does not exist in SampleSet %s', sampNum, sampleSet.sampleDir)
            return
        logging.debug('playing midi:%s sample#:%s %s', note, sampNum, sampleSet.sampleNames[sampNum])
        pygame.mixer.Channel(chan).play(sampleSet.sampleSounds[sampNum])

    def storeSeq(self, slot):  #store a seq to mem
        logging.info("Store to seqbank: %s", slot)
        self.seqList[slot] = copy.deepcopy(self.currSeq)

    def loadSeq(self, slot): #restore seq from mem
        logging.info("Loading seq from seqbank: %s", slot)
        self.currSeq = copy.deepcopy(self.seqList[slot])
        self.currSeqNum = slot 
        self.updateDisplay()

    def handleCtl(self, ctlEvent):  #control events from number pad
        #The modStar and modSlash mechanisms only work if the mod is pressed first.
        #Would be nicer to allow any order
        logging.debug("got control event:%s", ctlEvent)
        keyType = ctlEvent['keyType']
        modStar = ctlEvent['modStar']
        modSlash = ctlEvent['modSlash']
        keyVal = ctlEvent['keyVal']

        #beatsPerMinute rate adjust - [*/]+/-
        if keyType == KeyTypes.plus:
            bpmChange = 5
        if keyType == KeyTypes.minus:
            bpmChange = -5
        if keyType == KeyTypes.plus or keyType == KeyTypes.minus:
            if modStar:
                bpmChange *= 2
            if modSlash:
                bpmChange /= 5
            self.beatsPerMinute += bpmChange
            logging.debug("Beats per minute: %s", self.beatsPerMinute)
            self.updateDisplay()

        #metronome on/off - .
        if keyType == KeyTypes.dot:
            self.metroOn = not self.metroOn
            logging.debug("Metronome: %s", self.metroOn)

        #Enter
        if keyType == KeyTypes.enter:
            if not modStar: #Start/stop sequence
                if self.is_running:
                    self.stop()
                else:
                    self.start()
                logging.info("Sequence running: %s", self.is_running)
            else:   #store sequence to file
                self.currSeq.saveSequence()

        #BkSpc
        if keyType == KeyTypes.backspace:
            if not modStar: #Delete last note entered 
                logging.info("Delete last note")
                self.currSeq.delNote()
            else:
                logging.info("clear current sequence")
                self.currSeq.clear()

        #recording on/off - NumLk
        if keyType == KeyTypes.numlock_on:
            logging.info("Record on")
            self.recording = True
            self.updateDisplay()
        if keyType == KeyTypes.numlock_off:
            logging.info("Record off")
            self.recording = False
            self.updateDisplay()
        
        #store/restore seq to/from mem or change sample - 0-9
        if keyType == KeyTypes.num: 
            #logging.info("num: %s", keyVal)
            if modSlash:    #load sample
                if keyVal > len(sampMgr.sampleDirs):
                    logging.warning("Attempted to load sample: %s but does not exist", keyVal)
                else:
                    logging.info("Loading sample: %s", sampMgr.sampleDirs[keyVal])
                    sampMgr.currSampleDir = keyVal
                    self.updateDisplay()
            elif modStar:   #store currSeq to mem
                self.storeSeq(keyVal)
            else:   #load currSeq from mem
                if self.seqList[keyVal] is None:
                    logging.warning("No sequence stored at seqbank: %s", keyVal)
                else:
                    self.loadSeq(keyVal)

topSampleDir = '../samples/'
metroDir = topSampleDir + 'ZZ_Metronome/'
sampleExtension = '/*.wav'
class SampleSet():
    """ holds info for set of samples """
    def __init__(self, sampleDir):
        self.sampleDir = sampleDir
        self.samplePaths= glob.glob(topSampleDir + self.sampleDir + sampleExtension)
        self.samplePaths.sort()
        self.sampleNames = list()
        self.sampleSounds = list()
        for samp in self.samplePaths:
            snd = pygame.mixer.Sound(samp)
            self.sampleSounds.append(snd)
            m = re.search("([\w-]+)\.wav$", samp)  #find the short name
            if m is not None:
                name = m.group(1)
            else:
                name = "???"
            self.sampleNames.append(name)
        self.printMe()
    def printMe(self):  #there is probably an official print serialization term...
        #logging.info('sampleDir: %s', self.sampleDir)
        logging.info('sampleDir: %s\nSample Names%s', self.sampleDir, self.sampleNames)


class SampleMgr():
    """
    Load and manage lists of samples
    """
    def __init__(self):
        self.currSampleDir = 2
        self.sampleSets = list()

    def findSamples(self):
        """ search topSampleDir for all sample directories """

        self.metro = pygame.mixer.Sound(metroDir + 'metronome.wav')
        self.chime = pygame.mixer.Sound(metroDir + 'triangle10.wav')
        logging.info("Top sample directory: %s", topSampleDir)
        self.sampleDirs = list() #list of sample directory
        for root, dirs, files in os.walk(topSampleDir):
            for sampDir in dirs:
                self.sampleDirs.append(sampDir)
        self.sampleDirs.sort()

        for sampDir in self.sampleDirs:
            #fill out a sampleSet for this directory
            sampSet = SampleSet(sampDir)
            self.sampleSets.append(sampSet)

class KeyTypes(Enum):
    """ enumerate the types of keys in the num keypad """
    num = auto()
    plus = auto()
    minus = auto()
    dot = auto()
    enter = auto()
    backspace = auto()
    numlock_on = auto()
    numlock_off = auto()

backspace_key = 8
numlock_key = 300
class KeyEventHandler():
    """ 
    Parses numeric keypad events to be consumed by SeqManager

    Maintains state of modifier keys '*' and '/'
    Returns the key (if an active key) and modifier key states
    """
    def __init__(self):
        self.modStar = False
        self.modSlash = False
    def parseKey(self, event):
        #full list of pygame.K_KP_* to be found here:  http://pygame.org/docs/ref/key.html
        keyType = None
        keyVal = None
        if (event.type == pygame.KEYDOWN):
            try: #see if key is [0-9], pygame K_KPx is agnostic to NumLock state
                numRange = range(pygame.K_KP0, pygame.K_KP9+1).index(event.key)
            except ValueError: #nope
                numRange = None

            if (event.key == pygame.K_KP_MULTIPLY):
                self.modStar = True
            elif (event.key == pygame.K_KP_DIVIDE):
                self.modSlash = True
            elif (numRange != None): #this works regardless of numlock state
                #key = str(numRange)
                keyType = KeyTypes.num
                keyVal = numRange
            elif (event.key == pygame.K_KP_PERIOD):
                keyType = KeyTypes.dot
            elif (event.key == pygame.K_KP_PLUS):
                keyType = KeyTypes.plus
            elif (event.key == pygame.K_KP_MINUS):
                keyType = KeyTypes.minus
            elif (event.key == pygame.K_KP_ENTER):
                keyType = KeyTypes.enter
            elif (event.key == backspace_key): #can't find a mapping for backspace on the keypad
                keyType = KeyTypes.backspace
            elif (event.key == numlock_key): 
                keyType = KeyTypes.numlock_on

        elif (event.type == pygame.KEYUP): #if a modifier key, clear it
            if (event.key == pygame.K_KP_MULTIPLY):
                self.modStar = False
            elif (event.key == pygame.K_KP_DIVIDE):
                self.modSlash = False
            elif (event.key == numlock_key): 
                keyType = KeyTypes.numlock_off

        if keyType != None: #got an actionable event (vs a modifier key)
            keyEvent = {'keyType':keyType, 'keyVal':keyVal, 'modStar':self.modStar, 'modSlash':self.modSlash}
            logging.debug("got key:%s", keyEvent)
            return keyEvent

#rcdChar = "*"
#rcdChar = "\u00AE" #this is the (r) sign, but won't display correctly on LCD
#rcdChar = '\xF5'    #music note (extended ASCII) but doesn't work on LCD
rcdChar = "-R"

from serial import SerialException
class Display():
    def initDisplay(self):
        self.enabled = False
        self._lcd = lcdDisplay.lcdDisplay()
        try:
            self._lcd.init() #will fail if not plugged in
        except SerialException as e:
            logging.warning("Display LCD not found")
            return
        self.enabled = True   #handle case where not plugged in
        time.sleep(3) #Need to give it time to boot?
        self.writeStatic()
        self.updateTime(0,0)
        self.updateSettings(0,0,0,False)

    def writeStatic(self):  #fill the static elements
        self._lcd.write("[0-0] bpm:",1,0)   #last two params are line # (1 or 2) and cursor pos
        self._lcd.write("Seq:",2,0) 
        self._lcd.write("Samp:",2,8) 

    def updateTime(self, meas, beat):
        self._lcd.write(str(meas),1,1)
        self._lcd.write(str(beat),1,3) 

    def updateSettings(self, bpm, sampSet, seqNum, rcd):
        bpmStr = "{0:<3d}".format(bpm)
        self._lcd.write(bpmStr, 1,10)
        if rcd: rcding=rcdChar
        else: rcding=" "
        self._lcd.write(rcding,1,14)
        self._lcd.write(str(seqNum),2,4)
        self._lcd.write(str(sampSet),2,12)

savedSequenceDir = '../savedSequences/'
def main(argDict):
    #setup logging
    logLevel = LOG_LEVEL #default as specified statically
    if argDict['logLevel'] is not None:
        if argDict['logLevel'] == 'debug':
            logLevel = logging.DEBUG
    logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s', datefmt='%I:%M:%S %p', level=logLevel)
    print ("TESTING", file=sys.stderr)

    #handle system signals (seems to be an issue when autostarting via systemd)
    def handler(signum, frame):
        logging.warning("Got a {} signal.  Ignoring".format(signum))
    signal.signal(signal.SIGHUP, handler)   # signal 1
    #signal.signal(signal.SIGTERM, handler) # signal 15
    #signal.signal(signal.SIGCONT, handler) # signal 18

    #create the main objects
    global seqMgr, sampMgr, display #need to explicitly called out as global here because we are assigning them
    try:
        pySetup = PygameSetup()
    except Exception as ex:
        logging.warning("Got exception: {}".format(ex))

    display = Display()
    timeSigArgs = dict( (k, argDict[k]) for k in ('bpm', 'numMeasures', 'numBeats', 'numSubBeats') )

    sampMgr = SampleMgr()
    seqMgr = SequenceMgr(timeSigArgs)  #creates SeqTime, etc... Only pass relevant args 
    midi = ThreadedMidi()   #this kicks off the midi event handler

    if argDict['loadSeq'] is not None:
        fileName = savedSequenceDir + argDict['loadSeq']
        logging.info("Loading sequence file: " + fileName)
        seqMgr.currSeq=Sequence(fileName)
        seqMgr.storeSeq(0)  #store into mem slot 0 

    logging.info("About to start Pygame")
    #init everything
    pySetup.initPygame()
    logging.info("After init of Pygame")
    sampMgr.findSamples()
    display.initDisplay()
    seqMgr.updateDisplay()
    seqMgr.start()

    keyHandler = KeyEventHandler()

    #Start pygame event loop (keyboard input) - pygame docs say it is important this is in main thread
    while True:
        for event in pygame.event.get():
            #logging.debug("raw event:%s", event)
            if event.type == pygame.QUIT:
                logging.debug("Quit cmd")
                pygame.quit(); #sys.exit() if sys is imported
            keyEv = keyHandler.parseKey(event)
            if (keyEv != None):
                seqMgr.handleCtl(keyEv)


#autostart if called from cmd line "python3 _myname.py_"
if __name__ == "__main__":
    #For arg handling, simplistic sln is to build a dict from the cmd line args
    #argDict = dict(arg.split('=') for arg in sys.argv[1:])  #don't include the executable name

    #more powerful is to use argparse ==>
    parser = argparse.ArgumentParser(description="Interactive sample sequencer")
    parser.add_argument("--bpm",  type=int, help="# Beats per Minute")
    parser.add_argument("--numMeasures", type=int, help="# measures in sequence")
    parser.add_argument("--numBeats",  type=int, help="# Beats per Measure")
    parser.add_argument("--numSubBeats",  type=int, help="# Subbeats within beats")
    parser.add_argument("--loadSeq", help="load a json encoded sequence file from storedSequences.  Will also store in mem slot 0")
    parser.add_argument("--logLevel", help="Set the logging level")
    args = parser.parse_args()
    argDict = vars(args)
    print ("args: ", argDict)


    main(argDict)

