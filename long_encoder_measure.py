import time
import sys
import json
import numpy as np
from pathlib import Path
from controller.galil_controller import GalilController
from controller.data_models import AppSettings
from utils.logger_setup import setup_logger
from pipython import GCSDevice


CONTROLLER_ADDRESS = "COM3"  # Update with your controller port
AXIS = "A"
# OUTPUT_FILE = "galil_controller_suite/encoder_measurements.txt"
# OUTPUT_FILE_BACKLASH_CALC = "galil_controller_suite/encoder_average_backlash.txt"
OUTPUT_FILE = "encoder_measurements.txt"
OUTPUT_FILE_BACKLASH_CALC = "encoder_average_backlash.txt"
COUNTS_PER_MICRON = 6.4  # for motor
WAIT_TIME_AFTER_MOVE = 1  # Seconds to wait for motion to settle

controller = GalilController()
logger = setup_logger("encoder_measure")

controller.connect(CONTROLLER_ADDRESS)

controller.enable_axes(AXIS)
time.sleep(0.5)

# PI controller connection (for encoder reading)
pidevice = GCSDevice('E-873')
pidevice.InterfaceSetupDlg()
print('connected: {}'.format(pidevice.qIDN().strip()))

# Open output file
output_path = Path(OUTPUT_FILE)
with open(output_path, 'w') as f:
    # Write header
    f.write(f"Axis, Step size (um) , actual counts moved by encoder , displacement by encoder (um) , absolute encoder displacement (um), encoder error (um), actual error (um)\n")


output_path_backlash_calc = Path(OUTPUT_FILE_BACKLASH_CALC)
with open(output_path_backlash_calc, 'w') as f:
    # Write header
    f.write(f"Step, Averege Encoder Error (um), Error between Expected and Absolute (um)\n")


# this function performs a test to check the move_opposite_direction function
# to see if it compensates for the backlash correctly

def check_backlash_compensation_method():
    print(f"Performing backlash comp function check")
    ITERATIONS = 10
    STEP_SIZE = 300 # in microns

    # performs first movement in +X direction first

    print(f"moving in positive direction")
    relative_encoder_starting_pos = controller.poll_all_axes()[AXIS].pos
    optical_encoder_starting_pos = pidevice.qPOS('1')['1']

    controller.move_relative_closed_loop(AXIS, STEP_SIZE*COUNTS_PER_MICRON)
    time.sleep(0.1)
    
    # controller.move_relative_closed_loop(AXIS, STEP_SIZE*COUNTS_PER_MICRON/2)
    # time.sleep(1.5)

    relative_encoder_end_pos = controller.poll_all_axes()[AXIS].pos
    optical_encoder_end_pos = pidevice.qPOS('1')['1']

    counts_moved_by_relative_encoder = relative_encoder_end_pos - relative_encoder_starting_pos
    distance_moved_by_relative_encoder = counts_moved_by_relative_encoder / 6.4
    distance_moved_by_absolute_encoder = 1000*abs(optical_encoder_end_pos - optical_encoder_starting_pos)
    error = distance_moved_by_absolute_encoder - distance_moved_by_relative_encoder
    actual_error = STEP_SIZE - distance_moved_by_absolute_encoder

    
    with open(output_path, 'a') as f:
        f.write(f"{AXIS} , {STEP_SIZE} , {counts_moved_by_relative_encoder} , {distance_moved_by_relative_encoder} , {distance_moved_by_absolute_encoder}, {error}, {actual_error}\n")


    # now doing the back and forth movements iterations
    sign = 1

    for iterations in range(1,ITERATIONS,1):
        relative_encoder_starting_pos = controller.poll_all_axes()[AXIS].pos
        optical_encoder_starting_pos = pidevice.qPOS('1')['1']

        if sign == -1:
            controller.move_opposite_direction(AXIS, sign*STEP_SIZE*COUNTS_PER_MICRON)
        else:
            controller.move_relative_closed_loop(AXIS,sign*STEP_SIZE*COUNTS_PER_MICRON)
        time.sleep(0.4)

        relative_encoder_end_pos = controller.poll_all_axes()[AXIS].pos
        optical_encoder_end_pos = pidevice.qPOS('1')['1']
        print(f"iteration {iterations}: absolute encoder starting value is {optical_encoder_starting_pos}, end value is {optical_encoder_end_pos}")

        counts_moved_by_relative_encoder = relative_encoder_end_pos - relative_encoder_starting_pos
        distance_moved_by_relative_encoder = counts_moved_by_relative_encoder / 6.4
        distance_moved_by_absolute_encoder = 1000*abs(optical_encoder_end_pos - optical_encoder_starting_pos)*sign
        error = distance_moved_by_absolute_encoder - distance_moved_by_relative_encoder
        actual_error = sign*STEP_SIZE - distance_moved_by_absolute_encoder

        

        with open(output_path, 'a') as f:
            f.write(f"{AXIS} , {STEP_SIZE} , {counts_moved_by_relative_encoder} , {distance_moved_by_relative_encoder} , {distance_moved_by_absolute_encoder}, {error}, {actual_error}\n")

        # sign = -sign
    print(f"last movement was in: {-sign} direction")



# this function will move the motor 10 back and forth starting with STEP_COUNTS step
# after 10 times the step size increases by a factor and runs 10 times again.
# this continues until the step size equals to END_POINT

def long_encoder_measure():

    DISPLACEMENT_MICRONS = 100
    STEP_COUNTS = DISPLACEMENT_MICRONS * COUNTS_PER_MICRON  # Counts to move each iteration
    NUM_ITERATIONS = 6  # Number of move-measure cycles
    STEP_SIZE = 10
    END_POINT = 200
    MARCO_ITERATION = int(np.ceil((END_POINT - DISPLACEMENT_MICRONS) / STEP_SIZE)  + 1)

    sign = -1
    old_sign = 1
    average_actual_displacement_error = 0
    average_encoder_err = 0
    step = 0

    for macro_iterations in range(1,MARCO_ITERATION,1):
        print("macro iteration is: " , macro_iterations)
        for iteration in range(1,NUM_ITERATIONS,1):
            relative_encoder_starting_pos = controller.poll_all_axes()[AXIS].pos
            optical_encoder_starting_pos = pidevice.qPOS('1')['1']

            print("moving stage")
            steps_to_move = STEP_COUNTS + step*COUNTS_PER_MICRON
            steps_to_move = steps_to_move*sign

            if sign == 1:
                controller.move_relative_closed_loop(AXIS, steps_to_move)
                time.sleep(0.3)
            if sign == -1:
                controller.move_relative_closed_loop(AXIS, steps_to_move)
                time.sleep(0.3)

            relative_encoder_end_pos = controller.poll_all_axes()[AXIS].pos
            optical_encoder_end_pos = pidevice.qPOS('1')['1']

            encoder_displacement = (relative_encoder_end_pos - relative_encoder_starting_pos) / 6.4
            actual_displacement = sign*1000.0*(abs(optical_encoder_end_pos - optical_encoder_starting_pos)) # result is in um

            encoder_error = actual_displacement - encoder_displacement
            average_encoder_err += encoder_error

            actual_displacement_error = -1*DISPLACEMENT_MICRONS + step - actual_displacement
            average_actual_displacement_error += actual_displacement_error

            # Save to file
            with open(output_path, 'a') as f:
                f.write(f"{AXIS} , {DISPLACEMENT_MICRONS + step} , {encoder_displacement*6.4} , {encoder_displacement} , {actual_displacement}, {encoder_displacement}, {actual_displacement_error}\n")
            
            old_sign = sign
            # sign = -sign

        average_encoder_err = average_encoder_err / (NUM_ITERATIONS - 1)
        average_actual_displacement_error = average_actual_displacement_error / (NUM_ITERATIONS - 1)

        with open(output_path_backlash_calc, 'a') as f:
            f.write(f"{DISPLACEMENT_MICRONS + step} , {average_encoder_err}, {average_actual_displacement_error}\n")

        average_encoder_err = 0
        average_actual_displacement_error = 0
        step += STEP_SIZE

        if pidevice.qPOS('1')['1'] < 0.1:
            controller.move_relative_closed_loop(AXIS, 150*6.4)
            time.sleep(0.3)
            controller.move_relative_closed_loop(AXIS, -40*6.4)
            time.sleep(0.3)


def quantify_Error_for_step_size():
    step_sizes = [450,120,60,30,2,1] # in microns
    iterations = 10
    
    for i in range(len(step_sizes)):
        step_size = step_sizes[i]
        error_sum = 0
        error_between_expected_and_absolute = 0
        error_between_expected_and_absolute_sum = 0
        for j in range(iterations):
            relative_encoder_starting_pos = float(controller.send(f'MG _TPA').strip())
            optical_encoder_starting_pos = pidevice.qPOS('1')['1']

            controller.send(f'PR{AXIS}={step_size}')
            controller.send(f'BG{AXIS}')
            controller._g.GMotionComplete(AXIS)
            controller.send(f'WT={1}')
            time.sleep(0.1)

            relative_encoder_end_pos = float(controller.send(f'MG _TPA').strip())
            optical_encoder_end_pos = pidevice.qPOS('1')['1']

            counts_moved_by_relative_encoder = relative_encoder_end_pos - relative_encoder_starting_pos
            distance_moved_by_relative_encoder = counts_moved_by_relative_encoder / 8
            distance_moved_by_absolute_encoder = 1000*abs(optical_encoder_end_pos - optical_encoder_starting_pos)
            error = distance_moved_by_absolute_encoder - distance_moved_by_relative_encoder
            error_between_expected_and_absolute += step_size/6.4 - distance_moved_by_absolute_encoder
            error_between_expected_and_absolute_sum += error_between_expected_and_absolute
            error_sum += error


            with open(output_path, 'a') as f:
                f.write(f"{AXIS} , {step_size/6.4} , {counts_moved_by_relative_encoder} , {distance_moved_by_relative_encoder} , {distance_moved_by_absolute_encoder}, {error},\n")
            
        average_error = error_sum / iterations
        error_between_expected_and_absolute_avg = error_between_expected_and_absolute_sum / iterations

        with open(output_path_backlash_calc, 'a') as f:
                f.write(f"{step_size} , {average_error} , {error_between_expected_and_absolute_avg}\n")

        with open(output_path, 'a') as f:
                f.write(f"\n")
    
    controller.send(f'PR{AXIS}={-300*10*6.4}')
    controller.send(f'BG{AXIS}')
    controller._g.GMotionComplete(AXIS)
    controller.send(f'WT={1}')
    time.sleep(0.1)
        


# check_backlash_compensation_method()
long_encoder_measure()
# quantify_Error_for_step_size()

controller.disable_axes(AXIS)
controller.disconnect()
pidevice.CloseConnection()
print("finished program")