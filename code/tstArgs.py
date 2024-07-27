import sys
import argparse

#argDict = dict(arg.split('=') for arg in sys.argv[1:])  #don't include the executable name

for arg in sys.argv:
    print (arg)

parser = argparse.ArgumentParser(description="playing around with argument parsing")
parser.add_argument("--loadFile", action='append', help="specify a json filename to load (will be loaded into slot 0), or <filename>,<slotNum> to indicate file and a slot to load into" )
args = parser.parse_args()
argDict = vars(args)
print ("args: ", argDict)

fileArgs = argDict['loadFile']
for fileArg in fileArgs:
    if fileArg.find(',') is -1:
        print(f"Just the file: {fileArg}")
    else:
        (fileName, slotNum) = fileArg.split(',')
        print(f"File: {fileName}; slot: {slotNum}")


