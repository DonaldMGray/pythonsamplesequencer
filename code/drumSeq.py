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
import argparse
import json
import copy  #deep object copy
import lcdDisplay  #dmg - display module control
#from collections import namedtuple
from enum import Enum, auto

#set logging level
LOG_LEVEL = logging.DEBUG
#LOG_LEVEL = logging.INFO
PRINT_TIME = False

#The following are global for easy access:
#  seqMgr, sampMgr, display

"""
This is a drum sequence generator.

A 'sequence' consists of recorded notes in time.  A sequence has a length numMeasures * beatsPerMeasure * subBeats
Sequences can be saved and reloaded (to local mem).  Eventually, they can be saved/loaded from files.
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
- Load/Save Sequence to file


"""
logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s', datefmt='%I:%M:%S %p', level=LOG_LEVEL)

class PygameSetup():
    """
    Handle pygame initialization
    """
    def initPygame(self):
        # buffer size should be set low to ensure good timing; but too low can cause audio glitches
        # ~512 rcmd
        pygame.mixer.pre_init(frequency=22050, size=-16, channels=8, buffer=512)
        pygame.init()
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
            logging.error("MIDI DEVICE NOT CONNECTED")
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
    Holds time signature elements - beatsPerMinute, beatsPerMeasure, divisions per beat

    "isTock" is introduced to double the sampling rate (2x faster than the subbeat rate)
    This is important for capturing input - we want 2x sampling so we can "round" the capture time to be 
    closest to the right time point
    """
    def __init__(self, args):
        DEFAULT_BPM = 120
        DEFAULT_MEAS = 4
        DEFAULT_BEATSPERMEAS = 4
        DEFAULT_SUBBEATS = 2
        if args['bpm'] is not None:
            self.beatsPerMinute=int(args['bpm'])
        else:
            self.beatsPerMinute=DEFAULT_BPM
        if args['meas'] is not None:
            self.numMeasures=int(args['meas'])
        else:
            self.numMeasures=DEFAULT_MEAS
        if args['beatsPerMeas'] is not None:
            self.beatsPerMeasure=int(args['beatsPerMeas'])
        else:
            self.beatsPerMeasure=DEFAULT_BEATSPERMEAS
        if args['subBeats'] is not None:
            self.subBeats=int(args['subBeats'])
        else:
            self.subBeats = DEFAULT_SUBBEATS
        self.clearTime()
        self.numTicks = self.numMeasures * self.beatsPerMeasure * self.subBeats

    def clearTime(self):
        """ reset time clock """
        self.measure = self.numMeasures-1
        self.beat = self.beatsPerMeasure-1
        self.subBeat = self.subBeats-1
        self.isTock = True #isTock is true for the second half of the interval

    @property
    def beatsPerMinute(self):
        return self._beatsPerMinute
    @beatsPerMinute.setter
    def beatsPerMinute(self, value):
        if value < 10:
            value = 10
        self._beatsPerMinute = value

    def advanceTime(self):
        self.isTock = not self.isTock
        if not self.isTock:
            self.subBeat = (self.subBeat + 1) % self.subBeats
            if self.subBeat == 0:
                self.beat = (self.beat + 1) % self.beatsPerMeasure
                if self.beat == 0:
                    self.measure = (self.measure + 1) % self.numMeasures

    def printTime(self):
        print(self.measure+1, "-", self.beat+1, ":", self.subBeat+1, "  T:", self.tick)
        #logging.info('%s-%s:%s T:%s', self.measure+1, self.beat+1, self.subBeat+1, self.tick)

    @property
    def interval(self):
        """ time interval """
        return 60 / self.beatsPerMinute / self.subBeats / 2  #the 2 is because of "isTock"

    @property
    def tick(self):
       return (  self.measure*self.beatsPerMeasure*self.subBeats
                + self.beat*self.subBeats
                + self.subBeat)

    @property
    def roundedTick(self):
        #handles timing so input lines up properly to note time points
        if not self.isTock:
            return self.tick
        else:
            return (self.tick + 1) % self.numTicks


class Sequence():
    """ 
    A polyphonic sequence - [ONLY ONE VOICE PER TICK SUPPORTED CURRENTLY!!]
    Polyphonic uses the multiple channels of the mixer (8 total, one used by metronome)
    Note however that if two notes happen to be using the same channel and the first note is a long duration, it will get interrupted by the next note on that same channel
    """
    numPoly = 6  #how many simultaneous voices

    def clear(self):
        self.seqList = [list() for x in range(self.numTicks)]
        self._addedNotes = list()  #list of ticks where notes have been added to sequence (allows deleting in LIFO order)

    def __init__(self, numTicks):
        self.numTicks = numTicks
        self.boundSample = False  #TODO - if True, then save the sample used with the note.  ?use hasAttr to see if sample is stored?
        self.clear()

    @property
    def seqModified(self):
        return len(self._addedNotes) != 0

    def addNote(self, tick, note):
        if len(self.seqList[tick]) >= Sequence.numPoly:
            logging.debug("Overflowed poly on tick: %s", tick)
            del self.seqList[tick][0]
        self._addedNotes.append(tick)
        self.seqList[tick].append(note) 
        logging.debug('added: %s at: %s', note, tick)

    def delNote(self):
        #delete last note (remember to delete from _addedNotes)
        if len(self._addedNotes) == 0: return #no added notes to delete
        noteTick = self._addedNotes.pop()
        self.seqList[noteTick].pop()  #pop the last from _addedNotes; use that to index the tick, then pop from the voice list
        logging.debug('deleted from tick: %s', noteTick)

    def storeSeq(self):
        fileName = "../storedSequences/sequence" + datetime.datetime.now().strftime("%Y_%m_%d__%H_%M") + ".json"
        logging.info("Storing file: " + fileName)
        with open (fileName, mode="w") as file:
            json.dump(self.seqList, file, indent=4)
        os.chmod(fileName, 0o666)  #give write permission to all
        #print (json.dumps(self.seqList, indent=4) )    #print the string out


    def loadSeq(self):
        print ("not implemented")


chanUserInput = 6   #for "live" note input
chanMetro = 7   #which mixer channel to use for metronome

class SequenceMgr():
    """
    Manages all sequences & samples
    Responds to user events from keypad (not midi pad)
    """
    #This class is effectively a singleton, so not sure the "self._myvar" usage is needed.  Good hygene?
    def __init__(self, argDict):
        self._timer = None
        self.is_running = False
        self.metroOn = True
        self.seqTime = SeqTime(argDict)  #lazy to just pass all the args, but it can pick them out
        self.currSeq = Sequence(self.seqTime.numTicks)
        self.currSeqNum = 0
        self.recording = False
        self.seqList = [Sequence(self.seqTime.numTicks)] * 10   #pre-init list of sequences

    def start(self):
        """ sequence start/stop & callback handling """
        if not self.is_running:
            self.is_running = True
            self.next_call = time.time()
            self.seqTime.clearTime() #restart the time sequence
        else:
            self.next_call += self.seqTime.interval
        self._timer = threading.Timer(self.next_call - time.time(), self._run)
        self._timer.start()

    def stop(self):
        self._timer.cancel()
        self.is_running = False

    def _run(self):
        self.start() #set next timer trigger
        self.advanceSequence() #run the sequencer

    def updateDisplay(self):
        display.updateSettings(self.seqTime.beatsPerMinute, sampMgr.currSampleDir, self.currSeqNum, self.recording)

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
            seqTickNotes = self.currSeq.seqList[self.seqTime.tick]
            if len(seqTickNotes) != 0:
                self.mixChan=0
                if True:    #enable poly
                    for note in seqTickNotes:
                        self.playMidiNote(note, self.mixChan)
                        self.mixChan += 1
                else:  #no poly
                    self.playMidiNote(seqTickNotes[0],0) 

    def handleNoteIn(self,note):
        """
        Handle midi note pressed
        If recording, add to the current sequence
        """
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

    def handleCtl(self, ctlEvent):  #control events from number pad
        logging.debug("got control event:%s", ctlEvent)
        keyType = ctlEvent['keyType']
        modStar = ctlEvent['modStar']
        modSlash = ctlEvent['modSlash']
        keyVal = ctlEvent['keyVal']

        #beatsPerMinute rate adjust - [*-]+/-
        if keyType == KeyTypes.plus:
            bpmChange = 5
        if keyType == KeyTypes.minus:
            bpmChange = -5
        if keyType == KeyTypes.plus or keyType == KeyTypes.minus:
            if modStar:
                bpmChange *= 2
            if modSlash:
                bpmChange /= 5
            self.seqTime.beatsPerMinute += bpmChange
            logging.debug("Beats per minute: %s", self.seqTime.beatsPerMinute)
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
                    #self.currSeq.clear()
                else:
                    self.start()
                logging.info("Sequence running: %s", self.is_running)
            else:   #store sequence to file
                self.currSeq.storeSeq()

        #BkSpc
        if keyType == KeyTypes.backspace:
            if not modStar: #Delete last note entered 
                logging.info("Delete last note")
                self.currSeq.delNote()
            else:
                logging.info("clear current sequence")
                self.currSeq = Sequence(self.seqTime.numTicks)

        #recording on/off - NumLk
        if keyType == KeyTypes.numlock_on:
            logging.info("Record on")
            self.recording = True
            self.updateDisplay()
        if keyType == KeyTypes.numlock_off:
            logging.info("Record off")
            self.recording = False
            self.updateDisplay()
        
        #load/save seq - 0-9
        if keyType == KeyTypes.num: 
            logging.info("num: %s", keyVal)
            if modSlash:    #load sample
                if keyVal > len(sampMgr.sampleDirs):
                    logging.info("Attempted to load sample: %s but does not exist", keyVal)
                else:
                    logging.info("Loading sample: %s", sampMgr.sampleDirs[keyVal-1])
                    sampMgr.currSampleDir = keyVal-1
                    self.updateDisplay()
            elif modStar:   #save currSeq to mem
                logging.info("Saving to seqbank: %s", keyVal)
                self.seqList[keyVal] = copy.copy(self.currSeq)
            else:   #load currSeq from mem
                logging.info("Loading seq from seqbank: %s", keyVal)
                self.currSeq = copy.copy(self.seqList[keyVal])
                self.currSeqNum = keyVal
                self.updateDisplay()

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
        self.currSampleDir = 1
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
rcdChar = "(r"

from serial import SerialException
class Display():
    def initDisplay(self):
        self.enabled = False
        self._lcd = lcdDisplay.lcdDisplay()
        try:
            self._lcd.init() #will fail if not plugged in
        except SerialException as e:
            logging.info("Display LCD not found")
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


def main(args):
    #create the main objects
    global seqMgr, sampMgr, display #need to explicitly called out as global here because we are assigning them
    pySetup = PygameSetup()
    display = Display()
    #build a dict for the SeqTime args
    argDict = vars(args)
    print ("args: ", argDict)
    subDict = dict( (k, argDict[k]) for k in ('bpm', 'meas', 'beatsPerMeas', 'subBeats') )
    sampMgr = SampleMgr()
    seqMgr = SequenceMgr(subDict)  #creates SeqTime, etc... Only pass relevant args 
    midi = ThreadedMidi()   #this kicks off the midi event handler

    #init everything
    pySetup.initPygame()
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
    #simplistic sln is to build a dict from the cmd line args
    #argDict = dict(arg.split('=') for arg in sys.argv[1:])  #don't include the executable name

    #more powerful is to use argparse ==>
    parser = argparse.ArgumentParser(description="Interactive sample sequencer")
    parser.add_argument("--meas", type=int, help="# measures in sequence")
    parser.add_argument("--bpm",  type=int, help="Beats per Minute")
    parser.add_argument("--beatsPerMeas",  type=int, help="Beats per Measure")
    parser.add_argument("--subBeats",  type=int, help="# Subbeats within beats")
    parser.add_argument("--loadSeq", help="load a json encoded sequence file from storedSequences")
    args = parser.parse_args()

    main(args)





