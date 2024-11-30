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
import copy  #deep object copy
import lcdDisplay  #dmg - display module control
#from collections import namedtuple
from enum import Enum, auto
from functools import reduce

#set logging level
LOG_LEVEL = logging.INFO    #default; is adjustable via --logLevel
PRINT_TIME = False

#may help pygame??
os.environ["SDL_VIDEODRIVER"] = "dummy"

#The following are global for easy access:
#  seqMgr, sampMgr, display

"""
====================
    THE FOLLOWING COMMENTS ARE COPIED FROM THE MS WORD DOC
====================
A �sequence� consists of recorded notes in time.
There are numMeasures measures in the sequence; numBeats beats per measure; numSubBeats per beat.  T
he subbeats allow for beat smaller than the time signature eg: allow 1/8th notes in 4/4 time
The beatsPerMinute can also be adjusted

Sequences can be stored and loaded to/from local mem �banks� 0-9.
Sequences can also be saved to files (marked with current date/time) and also loaded from files (onl
y via cmd line)

The �CurrentSequence� is the one currently being edited � default empty unless specified when starte
d in cmd line

�Samples� allow mapping from notes to music samples.
The �current sample set� is the one being used by the current sequence at any one time.
The starting current Sample is the first sub-directory alphabetically in the Samples directory.
By default, samples are �unbound� to sequences, meaning the current sample can be changed and the cu
rrent sequence will then play using the (new) current sample.
Future: Allow a sequence to be �sample bound�, then the samples associated with the sequence notes a
re remembered and will not change when the current sample is changed.  (Requires storing index to sa
mple sets per note)

Polyphony (enable playing multiple notes simultaneously)
Mixer has 8 channels (0-7)
�	Dedicate ch7 to metronome
�	Dedicate ch6 to �live� note
�	0-5 are for sequence playback
In the rare circumstance that more than 6 notes are layered in a sequence at the same timespace, the
 oldest will be removed.
Issue: A note that plays for a long time (more than one sub-beat) will likely be cut short by the ne
xt note played on a later beat but same mixer channel.
TODO: Would need to detect that that mixer channel is still playing (how?) and shift up to next non-
playing channel

Control keys:
	Enter	Start/Stop play
	.	Metronome on/off
	+/-	bpm change +/- 5bpm
	* & +/- 	+/- 10bpm
	/ & +/-	+/- 1 bpm
	bkspc	Delete last note entered
	*-bkspc Clear current Sequence
	NumLk  Record on/off
	[0-9]	Load seq from mem bank 0-9
	*[0-9]	Store curr seq into mem bank 0-9
	/[0-9]	Use sample 0-9
	*-Enter   Save file to ../storedSequences + datetime

Scene 1-4 on nanoPad2� select scene (fully defined sequence, sample, timeSig)

Command line options
usage: sampleSeq.py [-h] [--bpm BPM] [--numMeasures NUMMEASURES]
                    [--numBeats NUMBEATS] [--numSubBeats NUMSUBBEATS]
                    [--swingTime] [--loadSeq LOADSEQ] [--logLevel LOGLEVEL]

Interactive sample sequencer

====================
    END: THE FOLLOWING COMMENTS ARE COPIED FROM THE MS WORD DOC
====================
"""

NUM_MIXER_CHANNELS = 16
class PygameSetup():
    """
    Handle pygame initialization
    """
    def initPygame(self):
        # buffer size should be set low to ensure good timing; but too low can cause audio glitches
        # Buffer size ~512 rcmd
        # Should be able to support 16 chans
        pygame.mixer.pre_init(frequency=22050, size=-16, channels=8, buffer=512)
        #pygame.init()  #don't init all of pygame as this gets us into issues with the display
        pygame.mixer.init()
        pygame.mixer.set_num_channels(NUM_MIXER_CHANNELS) #even though we asked for 16 chans in the pre-init, seems to only have 8, so increase them now
        res = pygame.mixer.set_reserved(1) #first channel reserved for metronome
        logging.info("reserved channels: %s", res)  #doesn't seem to reserve...
        #keyboard event handling - only looking for keyboard up/down (not mouse)
        pygame.display.set_mode()
        pygame.event.set_allowed(None)
        pygame.event.set_allowed(pygame.KEYDOWN)
        pygame.event.set_allowed(pygame.KEYUP)

#needs to be set for the specific Midi input device
#midiNoteList = [36,37,38,39, 40,41,42,43, 44,45,46,47, 48,49,50,51] #this is set of 16 notes for scene 0.  Other scenes continue notes from here (modulus 16).  eg: 52-67
MIDI_FIRST_NOTE = 36 #The first note - scene0:36-51; scene1:52-67; scene2: 68-83; scene3: 84-99
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
            logging.debug("Midi msg==> %s", msg)
            if msg.type == "sysex":
                try:
                    logging.debug(f"sysex msg: {msg.data}")
                    seqMgr.handleSceneChange(msg.data[-1])
                except AttributeError:
                    pass #catch condition where the scene change button is hit while still starting up
            elif msg.type == "note_on":
                logging.debug("Midi note_on ==> %s", msg.note)
                seqMgr.handleNoteIn(msg.note)

class SeqTime():
    """
    Holds current time elements - measure; beat, sub-beat

    "isTock" is introduced to double the sampling rate (2x faster than the subbeat rate)
    This is important for capturing input - we want 2x sampling so we can "round" the capture time to be 
    closest to the right time point
    """
    def __init__(self, seq):
        self.timeSig = seq.sequence['timeSig']
        self.numTicks = seq.numTicks
        self.clearTime()

    def clearTime(self):  #set all values to just b4 zero so that first advance will cleanly start at zero
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
            return (self.tick + 1) % self.numTicks


DEFAULT_BPM = 120
DEFAULT_NUMMEAS = 4
DEFAULT_NUMBEATSPERMEAS = 4
DEFAULT_NUMSUBBEATS = 2

"""
seqChanStart = 0
liveChanStart = 10   #for "live" note input
numLiveChan = 4
"""
numSeqChan = 8  #how many simultaneous voices are allowed (per tick)
chanMetro = 0   #which mixer channel to use for metronome.  This is a reserved channel

class Sequence():
    """ 
    A polyphonic sequence 
    Polyphonic uses the multiple channels of the mixer (8 total, one used by metronome, one for real-time input)
    Note however that if two notes happen to be using the same channel and the first note is a long duration, it will get interrupted by the next note on that same channel
    This issue can be observed for example when using the tabla sample set
    """

    def __init__(self, arg=None):
        if isinstance(arg, dict):   #dictionary of time signature elements
            timeSig = self.createTimeSig(arg)
            self.initSequence(timeSig)
        elif isinstance(arg, str):  #must be a filename to load
            with open (arg, mode="r") as jsonFile:
                loadedSeq=json.load(jsonFile)
            timeSig = self.createTimeSig(loadedSeq['timeSig'])
            logging.info(f"timeSig: {timeSig}")
            self.initSequence(timeSig)
            self.sequence['noteList']=loadedSeq['noteList']
            logging.info("loaded sequence from: {0}\n {1} {2}".format(arg, loadedSeq['timeSig'], loadedSeq['noteList']) )
        else:
            raise TypeError('Type: {0} not supported'.format(type(arg)))

    def createTimeSig(self, timeSigArgs):
        timeSig = dict()
        if timeSigArgs['numMeasures'] is not None:
            timeSig['numMeasures']=int(timeSigArgs['numMeasures'])
        else:
            timeSig['numMeasures']=DEFAULT_NUMMEAS
        if timeSigArgs['numBeats'] is not None:
            timeSig['numBeats']=int(timeSigArgs['numBeats'])
        else:
            timeSig['numBeats']=DEFAULT_NUMBEATSPERMEAS
        if timeSigArgs['numSubBeats'] is not None:
            timeSig['numSubBeats']=int(timeSigArgs['numSubBeats'])
        else:
            timeSig['numSubBeats']=DEFAULT_NUMSUBBEATS
        return timeSig

    @property
    def numTicks(self):
        timeSig = self.sequence['timeSig']
        return timeSig['numMeasures'] * timeSig['numBeats'] * timeSig['numSubBeats']

    def initSequence(self, timeSig):
        if timeSig is None: timeSig=self.createTimeSig()
        self.sequence = dict()
        self.sequence['timeSig'] = timeSig
        self.clearSequence()

    def clearSequence(self):
        self.sequence['noteList'] = [list() for x in range(self.numTicks)]
        self._addedNotes = list()  #list of ticks where notes have been added to sequence (allows deleting in LIFO order)

    @property
    def seqModified(self):
        return len(self._addedNotes) != 0

    def addNote(self, tick, note):
        noteList = self.sequence['noteList']
        if len(noteList[tick]) >= numSeqChan:
            logging.warn("Overflowed poly on tick: %s", tick)
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
        fileName = "savedSequences/sequence" + datetime.datetime.now().strftime("%Y_%m_%d__%H_%M") + ".json"
        logging.info("Saving sequence file: " + fileName)
        with open (fileName, mode="w") as jsonFile:
            json.dump(self.sequence, jsonFile, indent=4)
        os.chmod(fileName, 0o666)  #give write permission to all
        #print (json.dumps(self.sequence, indent=4) )    #print the string out



#Mixer channel allocation

savedSequenceDir = 'savedSequences/'
class SequenceMgr():
    """
    Manages all sequences & samples
    Responds to user events from keypad (not midi pad)
    """
    #This class is effectively a singleton, so not sure the "self._myvar" usage is needed.  Good hygene?
    def __init__(self, timeArgDict, swingTime=True):
        if timeArgDict['bpm'] is not None:
            self.beatsPerMinute=int(timeArgDict['bpm'])
        else:
            self.beatsPerMinute=DEFAULT_BPM
        self.currSeq = Sequence(timeArgDict)
        self.swingTime = swingTime
        self.currSeqNum = 0
        self.is_running = False
        self.metroOn = True
        self.recording = False
        #self.seqList = [Sequence(timeArgDict)] * 10   #pre-init list of sequences in mem
        self.seqList = [None] * 10   #pre-init list of sequences in mem

    def start(self):
        """ sequence start/stop & callback handling """
        if not self.is_running:
            self.is_running = True
            self.next_call = time.time()
        else:
            self.next_call += self.interval
            #print(f"beat:{self.seqTime.beat}; subBeat:{self.seqTime.subBeat}; timeInterval:{self.interval}")
        self._timer = threading.Timer(self.next_call - time.time(), self._run)
        self._timer.start()

    def stop(self):
        self._timer.cancel()
        self.is_running = False

    def _run(self):
        #bump the time - do this first so that everything is lined up to the new tick
        self.seqTime.advanceTime()
        self.start() #set next timer trigger
        self.advanceSequence() #run the sequencer - play all notes in this tick

    @property 
    def currSeq(self):
        return self._currSeq
    @currSeq.setter
    def currSeq(self, value):
        self._currSeq = value
        self.seqTime = SeqTime(value)

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
        """ In "straight time" the subbeats are 50% of the beats (eg: eighth notes in 4/4 time)
            In "swing" the subbeats are 2/3 (and 1/3) of the beats (or more/less)
            So to make swing we change the interval size by (2/3 / 0.5) and (1/3 / 0.5) 
        """
        straightTimeInterval =  60 / self.beatsPerMinute / self.seqTime.timeSig['numSubBeats'] / 2  #the 2 is because of "isTock"
        if not self.swingTime:
            timeInterval = straightTimeInterval
        else: #swing
            if self.seqTime.subBeat % 2 == 0:
                timeInterval = straightTimeInterval * 2/3 / 0.5
            else:
                timeInterval = straightTimeInterval * 1/3 / 0.5
        return timeInterval

    def updateDisplay(self):
        display.updateSettings(self.beatsPerMinute, sampMgr.currSampleDir, self.currSeqNum, self.recording)

    def advanceSequence(self):
        #update the display
        display.updateTime(self.seqTime.measure+1, self.seqTime.beat+1) #I think the +1's are to shift from zero-based numbering

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
                    metroChan = pygame.mixer.Channel(chanMetro)
                    metroChan.set_volume(metroVol)
                    metroChan.play(metroSamp);

            #Now play all the notes in this tick
            seqNoteList = self.currSeq.sequence['noteList']
            try:
                seqTickNotes = seqNoteList[self.seqTime.tick]
                if len(seqTickNotes) != 0:
                    for note in seqTickNotes:
                        self.playMidiNote(note) 
            except IndexError:
                logging.warning('Tick count is longer than sequence note list')

    #may capture all the scene deets in a struct (eg: BPM, ....) instead of just the filename
    #rename as PRESET
    #move these to be global...
    SEQ_SCENE0 = savedSequenceDir + "rock.json"
    SEQ_SCENE1 = savedSequenceDir + "swing2.json"
    SEQ_SCENE2 = savedSequenceDir + "4x4_funk.json"


    def handleSceneChange(self, scene):
        logging.info(f'Scene: {scene}')

        if scene == 0:
            self.stop()
            self.currSeq=Sequence(SequenceMgr.SEQ_SCENE0)  #means the last one specified will be played
            currSampleDir = sampMgr.index("PearlKitMapped")
            self.beatsPerMinute = 180
            self.swingTime = False
            self.updateDisplay()
            self.start()
        elif scene == 1:
            self.stop()
            self.currSeq=Sequence(SequenceMgr.SEQ_SCENE1)  #means the last one specified will be played
            currSampleDir = sampMgr.index("PearlKitMapped")
            self.beatsPerMinute = 120
            self.swingTime = True
            self.updateDisplay()
            self.start()
        elif scene == 2:
            self.stop()
            self.currSeq=Sequence(SequenceMgr.SEQ_SCENE2)  #means the last one specified will be played
            currSampleDir = sampMgr.index("PearlKitMapped")
            self.beatsPerMinute = 130
            self.swingTime = False
            self.updateDisplay()
            self.start()

    #for "live" notes (vs those recorded in sequence)
    def handleNoteIn(self,note):
        """
        Handle midi note pressed
        If recording, add to the current sequence
        """

        logging.debug("Handling note %s", note)
        if self.recording and self.seqTime.isTock:  #avoid the double play that happens if we enter a note in the Tock while recording (because the recording will play it the 2nd time)
            #can be a bit disconcerting with really slow tempo, but I think is better experience
            pass
        else:
            #print(f"playing note {note}")
            self.playMidiNote(note)  #Poly allowed for chans [0:5], metronome is chan 7
            """ debug channel alloc (see if reserved works)
            for chan in range(NUM_MIXER_CHANNELS):
                busy = pygame.mixer.Channel(chan).get_busy()
                logging.debug("chan %s busy: %s", chan, busy)
            """

        #add note to sequence
        if self.recording:
            noteTick = self.seqTime.roundedTick
            self.currSeq.addNote(noteTick, note)

    def playMidiNote(self,note):
        """ Based on midi input msg, play a sound """
        try:
            sampIndx = (note-MIDI_FIRST_NOTE) % 16  #mod 16 is because notes in alt scenes on nanoPad2 progressively higher up
        except ValueError: #
            return
        sampleSet = sampMgr.sampleSets[sampMgr.currSampleDir]
        if len(sampleSet.sampleSounds) < sampIndx+1:
            logging.debug('Sample %s does not exist in SampleSet %s', sampIndx, sampleSet.sampleDir)
            return
        logging.debug('playing midi:%s sample#:%s %s', note, sampIndx, sampleSet.sampleNames[sampIndx])
        sound = sampleSet.sampleSounds[sampIndx]
        chan = pygame.mixer.find_channel(True) #force it to find a channel (will kick off oldest note)
        #chan.play(sound)
        sound.play()    #this appears to be the only way to get a channel that isn't reserved (find_channel will find any unavailable chan)
        """ use to check channel allocation (first N chans are reserved)
        for chan in range(NUM_MIXER_CHANNELS):
            print("chan: %s -- busy: %s", chan, pygame.mixer.Channel(chan).get_busy())
        """
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
                self.currSeq.clearSequence()

        #recording on/off - NumLk
        if keyType == KeyTypes.numlock_on:
            logging.info("Record on")
            self.recording = True
            self.updateDisplay()
        if keyType == KeyTypes.numlock_off:
            logging.info("Record off")
            self.recording = False
            self.updateDisplay()
        
        #Number '0-9' - load sample or store/load seq to/from mem or change sample - 0-9
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

topSampleDir = 'samples/'
metroDir = topSampleDir + 'ZZ_Metronome/'
sampleExtension = '/*.wav'
class SampleSet():
    """ holds info for set of samples """
    def __init__(self, sampleDir):
        self.sampleDir = sampleDir
        self.samplePaths= glob.glob(topSampleDir + self.sampleDir + sampleExtension)
        self.samplePaths.sort()
        self.sampleNames = list()  #kludgy - names & sounds should be a tuple
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

    def index(self, name):  #given a name (string) return the sample list index 
        for sampSet in self.sampleSets:
            if sampSet.sampleDir == name:
                return self.sampleSets.index(sampSet)
        return None

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
        else: rcding="  "
        self._lcd.write(rcding,1,14)
        self._lcd.write(str(seqNum),2,4)
        self._lcd.write(str(sampSet),2,12)

def main(argDict):
    #setup logging
    logLevel = LOG_LEVEL #default as specified statically
    if argDict['logLevel'] is not None:
        if argDict['logLevel'] == 'debug':
            logLevel = logging.DEBUG
    logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s', datefmt='%I:%M:%S %p', level=logLevel)

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

    #load all the cmd args regarding timing (bpm, numMeasures, numBeats, numSubBeats)
    timeSigArgs = dict( (k, argDict[k]) for k in ('bpm', 'numMeasures', 'numBeats', 'numSubBeats') )

    #determine if any args are set which set timeSig (including loading a file)
    # if not, play the default
    checkArgList = timeSigArgs.copy()
    swingArgString = "swingTime"
    checkArgList[swingArgString] = argDict[swingArgString]
    loadSeqArgString = "loadSeq"
    checkArgList[loadSeqArgString] = argDict[loadSeqArgString]

    argsAreNone = list(map(lambda key: argDict[key] == None, checkArgList.keys() ))
    useDefault = reduce( lambda a, b: a and b, argsAreNone ) #indicates whether to play the default sequence and time sig

    ################
    #init everything
    ################
    midi = ThreadedMidi()   #this kicks off the midi event handler
    logging.info("About to start Pygame")
    pySetup.initPygame()
    logging.info("After init of Pygame")
    sampMgr = SampleMgr()
    sampMgr.findSamples()
    display.initDisplay()
    seqMgr = SequenceMgr(timeSigArgs, argDict['swingTime'])  #creates SeqTime, etc... Only pass relevant args 
    seqMgr.updateDisplay()
    seqMgr.start()
    keyHandler = KeyEventHandler()

    #load sequence file(s)
    if useDefault:  #no relevant args specified, so run default mode (play a scene)
        fileName = seqMgr.SEQ_SCENE0
        logging.info("Loading sequence file: " + fileName)
        seqMgr.currSeq=Sequence(fileName)  
        seqMgr.storeSeq(0)  #store into mem slot 0 
    elif argDict['loadSeq'] is not None:
        fileArgs = argDict['loadSeq']
        for fileArg in fileArgs: #loadSeq arg is a list (append option) because we want to allow multiple instances of it
            if fileArg.find(',') == -1: #just specified a file
                fileName = fileArg
                slotNum = 0
            else:   #specified a file and slot number to store it in
                (fileName, slotNum) = fileArg.split(',')
            #fileName = savedSequenceDir + fileName  #NO - don't prepend a dir, let user specify
            logging.info("Loading sequence file: " + fileName)
            seqMgr.currSeq=Sequence(fileName)  #means if slots not individually specified, the last one specified will be played
            seqMgr.storeSeq(int(slotNum))  #store into mem slot  


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
    parser = argparse.ArgumentParser(description="Interactive sample sequencer.  Default time sig is 4-4 @ 120bpm")
    parser.add_argument("--bpm",  type=int, help="# Beats per Minute")
    parser.add_argument("--numMeasures", type=int, help="# measures in sequence")
    parser.add_argument("--numBeats",  type=int, help="# Beats per Measure")
    parser.add_argument("--numSubBeats",  type=int, help="# Subbeats within beats")
    parser.add_argument("--swingTime", action='store_true', help="Use swing time")
    parser.add_argument("--loadSeq", action='append', help="load a json encoded sequence file from storedSequences. If only filename specified, will load into slot 0, can also specify slot vis --loadSeq=<fileName>,<slotNum>.  Also supports multiple files loaded to multiple slots (tested??)") 
    parser.add_argument("--logLevel", help="Set the logging level")
    args = parser.parse_args()
    argDict = vars(args)
    print ("args: ", argDict)


    main(argDict)

