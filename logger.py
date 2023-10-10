import logging

def configure_logger():
    logging.basicConfig(filename='/opt/gate-controller/logs/check-plate-and-open-gate.log',
                        level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    # Get the root logger
    logger = logging.getLogger()

    # Create a handler for console output
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)

    # Add the console handler to the root logger
    logger.addHandler(console_handler)

    return logger
