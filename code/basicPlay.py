import time
import pygame

pygame.mixer.pre_init(22050, -16, 8, 512)
#pygame.mixer.pre_init(44100, -16, 2, 512)
pygame.init()
#metro = pygame.mixer.Sound("metronome.wav")
metro = pygame.mixer.Sound("808/001.wav")
pygame.mixer.Channel(0).set_volume(1)

while (True):
    print("beep")
    pygame.mixer.Channel(0).play(metro)
    time.sleep(1)
    

