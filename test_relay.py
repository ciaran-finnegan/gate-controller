import PiRelay
import time

# Initialize PiRelay
r1 = PiRelay.Relay("RELAY1")

# Make a PiRelay call to open the gate
def make_pirelay_call():
    try:
        r1.on()
        time.sleep(1)
        r1.off()
        print(f'PiRelay call made to open the gate')
    except Exception as e:
        print(f'Error making PiRelay call: {str(e)}')

make_pirelay_call()