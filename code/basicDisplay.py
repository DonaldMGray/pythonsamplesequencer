import time
#import Pyserial 
import serial

#open serial communication
#time.sleep(1)
serialport = serial.Serial('/dev/ttyUSB0',9600, timeout=1) #will raise SerialException if not avail
time.sleep(.5)

#clear screen
serialport.write(b'\xfe\x01')
time.sleep(1)  #needed because clearing takes time (and is non-blocking).  Even 0.5 secs is not enough time strangely
serialport.write(b'Raspberry PI    ')
serialport.write(bytes('/dev/ttyUSB0    ','UTF-8'))
time.sleep(1)

#switch screen off
serialport.write(b'\xfe\x08')
time.sleep(1)

#switch screen on
serialport.write(b'\xfe\x0c')
time.sleep(1)

#scroller
serialport.write(b'\xfe\x01')
serialport.write(b'_-==scroller==-_ ')
for num in range(0,16):
    serialport.write(b'\xfe\x1c')
    time.sleep(0.2)	
for num in range(0,16):
    serialport.write(b'\xfe\x18')
    time.sleep(0.2) 
#close connection
serialport.close()

