"""
Encoder error measurement program.
Moves axis A by configurable step sizes and measures the actual vs. commanded positions.
Saves results to a .txt file.
"""

import time
import sys
import json
import numpy as np
from pathlib import Path
from controller.galil_controller import GalilController
from controller.data_models import AppSettings
from utils.logger_setup import setup_logger
from pipython import GCSDevice

# Configuration
CONTROLLER_ADDRESS = "COM3"  # Update with your controller port
AXIS = "A"
DISPLACEMENT_MICRONS = 30
COUNTS_PER_MICRON = 6.4  # for motor
STEP_COUNTS = DISPLACEMENT_MICRONS * COUNTS_PER_MICRON  # Counts to move each iteration
NUM_ITERATIONS = 12  # Number of move-measure cycles
OUTPUT_FILE = "encoder_measurements.txt"
WAIT_TIME_AFTER_MOVE = 1  # Seconds to wait for motion to settle
MOTOR_BACKLASH = 28

# Setup logger
logger = setup_logger("encoder_measure")

def load_settings() -> AppSettings:
    """Load motion parameters from config/settings.json."""
    config_path = Path(__file__).parent / "config" / "settings.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        settings = AppSettings(**data)
        logger.info(f"Loaded settings from {config_path}")
        return settings
    except Exception as e:
        logger.warning(f"Failed to load settings from {config_path}: {e}")
        logger.info("Using default settings")
        return AppSettings()


def main():
    settings = load_settings()
    axis_settings = settings.axis_settings(AXIS)

    controller = GalilController()
    
    try:
        # Connect to controller
        logger.info(f"Connecting to {CONTROLLER_ADDRESS}...")
        info = controller.connect(CONTROLLER_ADDRESS)
        logger.info(f"Connected: {info.model} Rev {info.revision}, Serial: {info.serial}")

        # Configure motor parameters from settings
        logger.info(f"Configuring motor parameters for axis {AXIS}...")
        controller.set_params(
            AXIS,
            speed=axis_settings.speed,
            accel=axis_settings.accel,
            decel=axis_settings.decel,
            err_limit=axis_settings.error_limit,
        )
        time.sleep(0.2)
        
        # Enable motors
        logger.info(f"Enabling axis {AXIS}...")
        controller.enable_axes(AXIS)
        time.sleep(0.5)

        # PI controller connection (for encoder reading)
        pidevice = GCSDevice('E-873')
        pidevice.InterfaceSetupDlg()
        print('connected: {}'.format(pidevice.qIDN().strip()))
        position = pidevice.qPOS('1')['1']
        print('position is: {}'.format(position)) 
        
        # Open output file
        output_path = Path(OUTPUT_FILE)
        with open(output_path, 'w') as f:
            # Write header
            f.write(f"Axis,Step size (um) , actual counts moved , actual displacement (um) , absolute encoder displacement (um), error (um)\n")
            logger.info(f"Output file: {output_path}")
        
        # Perform move-measure cycles
        logger.info(f"Starting {NUM_ITERATIONS} move-measure cycles...")
        logger.info(f"Moving {STEP_COUNTS} counts per iteration\n")

        difference = 0
        sign = 1.0
        old_sign = 1.0
        step = 0
        average = 0
        for iteration in range(1, NUM_ITERATIONS + 1):
            try:
                # Get initial position before move
                pos_before_relative_encoder = controller.poll_all_axes()[AXIS].pos
                logger.info(f"[Iteration {iteration}] Position before move: {pos_before_relative_encoder:.0f} counts")

                pos_before_absolute_encoder = pidevice.qPOS('1')['1']
                logger.info(f"[Iteration {iteration}] Absolute encoder position before move: {pos_before_absolute_encoder:.0f} mm")
                
                # Move axis A by STEP_COUNTS
                logger.info(f"[Iteration {iteration}] Issuing move command: {STEP_COUNTS} counts...")
                steps_to_move = sign * STEP_COUNTS
                # print("iteration: {} --->  steps to move: {}",format(iteration,steps_to_move))
                controller.move_relative_closed_loop(AXIS, steps_to_move)
                time.sleep(1)
                
                # Measure actual position after move
                pos_after_relative_encoder = controller.poll_all_axes()[AXIS].pos
                pos_after_absolute_encoder = pidevice.qPOS('1')['1']

                counts_actually_moved = pos_after_relative_encoder - pos_before_relative_encoder
                distance_actually_moved = counts_actually_moved / 6.4 # result is in microns
                distance_with_backlash = distance_actually_moved - MOTOR_BACKLASH*sign

                if old_sign != sign:
                    distance_to_show = distance_with_backlash
                else:
                    distance_to_show = distance_actually_moved

                absolute_encoder_measurement = sign*1000.0*(abs(pos_after_absolute_encoder - pos_before_absolute_encoder)) # result is in um

                error = abs(distance_to_show - absolute_encoder_measurement)

                # Save to file
                with open(output_path, 'a') as f:
                    f.write(f"{AXIS} , {DISPLACEMENT_MICRONS - step} , {counts_actually_moved} , {distance_to_show} , {absolute_encoder_measurement}, {error}\n")
                
            except Exception as e:
                logger.error(f"[Iteration {iteration}] Error: {e}")
                continue
            old_sign = sign
            sign = -sign  # Alternate direction for next move
            # step += 10
            # with open(output_path, 'a') as f:
            #     f.write(f"\n")
            #     f.write(f"\n")
            
            logger.info(f"Measurement complete. Results saved to {output_path}")
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    
    finally:
        # Cleanup
        try:
            logger.info("Disabling motors and disconnecting...")
            controller.disable_axes(AXIS)
            controller.disconnect()
            logger.info("Disconnected")
            pidevice.CloseConnection()
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")






if __name__ == "__main__":
    main()
