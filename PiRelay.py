#!/usr/bin/python

# Library for PiRelay V2
# Developed by: SB Components
# Author: Satyam
# Project: PiRelay-V2
# Python: 3.7.3


import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)

class Relay:
    ''' Class to handle Relay

    Arguments:
    relay = string Relay label (i.e. "RELAY1","RELAY2","RELAY3","RELAY4")
    '''
    relaypins = {"RELAY1":35, "RELAY2":33, "RELAY3":31, "RELAY4":29}


    def __init__(self, relay):
        self.pin = self.relaypins[relay]
        self.relay = relay
        GPIO.setup(self.pin,GPIO.OUT)
        GPIO.output(self.pin, GPIO.LOW)

    def on(self, pre_activation_inhibit=None):
        if pre_activation_inhibit is not None:
            inhibition = pre_activation_inhibit()
            if inhibition is not None:
                return inhibition
        GPIO.output(self.pin,GPIO.HIGH)
        return None

    def off(self):
        GPIO.output(self.pin,GPIO.LOW)
