import threading
import mido

midiDev = 1

ins = mido.get_input_names()
print(ins)
inport = mido.open_input(ins[midiDev])
for msg in inport:
    print(msg)
