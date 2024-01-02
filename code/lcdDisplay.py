import time
import serial

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
            posCmd = bytes([cmds.l1pos + pos])
        else:
            posCmd = bytes([cmds.l2pos + pos])
        self.serialport.write(cmdSym + posCmd)  #set the cursoe position
        self.serialport.write(string.encode())  #the encode ensures it is UTF-8 (bytes)

    def __exit__(self):
        #close connection
        print("exit called")
        self.serialport.close()



