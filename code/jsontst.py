import json

data = {
        "meas" : 4,
        "beat" : 2,
        "seqList" : [
            [1, 2],
            [3],
            [4]
            ]
        }

print (json.dumps(data))

data2 = {
        "timeSig" : {
            "meas" : 4,
            "beat" : 2
            },
        "seqList" : [
            [1, 2],
            [3],
            [4]
            ]
        }

print (json.dumps(data2))

class A:
    def __init__(self):
        self.prtA()
        self.b = self.B()
        self.b.prt()

    def prtA(self):
        print ("A class")

    class B:
        def prt(self):
            print ("B class")
            #self.prtA()

a = A()
