import time
import serial
import threading

cmdSym = b'\xfe'
class cmds():
    #clr = b'\x01'
    clr = 0x01
    off = 0x08
    on  = 0x0C
    shiftR = 0x1C
    shiftL = 0x18
    l1pos = 0x80
    l2pos = 0xC0
    
usbAddr = '/dev/ttyUSB0'
baudRate = 9600
class lcdDisplay():
    def init(self):
        self.lock = threading.Lock()
        #open serial communication
        self.serialport = serial.Serial(usbAddr, baudRate, timeout=1)

    def sendCommand(self, cmd):
        self.serialport.write(cmdSym + bytes([cmd]))    #bytes9[foo]) is how to convert number to bytes

    def clear(self):
        self.sendCommand(cmds.clr)

    def off(self):
        self.sendCommand(cmds.off)

    def on(self):
        self.sendCommand(cmds.on)

    def write(self, string, line=1, pos=0):
        if line==1:
            cmdLine = cmds.l1pos
        else:
            cmdLine = cmds.l2pos
        posCmd = bytes([cmdLine + pos])
        self.lock.acquire()  #ensure thread safety.  Can also specify "blocking" or "timeout"
        self.serialport.write(cmdSym + posCmd)  #set the cursor position
        self.serialport.write(string.encode())  #the encode ensures it is UTF-8 (bytes)
        self.lock.release()

    def __exit__(self):
        #close connection
        print("exit called")
        self.serialport.close()



