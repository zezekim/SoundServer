import serial
import time

SERIAL_PORT = "/dev/ttyS0"
BAUD_RATE = 115200 # Corrected Baud Rate

print(f"--- TESTING WITH BAUD RATE: {BAUD_RATE} ---")

ser = None
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) # Increased timeout slightly for stability
    print(f"Successfully opened serial port {SERIAL_PORT} at {BAUD_RATE} baud.")
    
    # Give modem time to boot after port open
    print("Waiting for modem to settle after port open (3s)...")
    time.sleep(3) 
    ser.reset_input_buffer() # Clear any boot messages

    def send_at_test(command, expected_ok="OK", expected_custom=None, timeout=3):
        print(f"\nSending: {command.strip()}")
        ser.write((command + "\r\n").encode())
        
        response_lines = []
        start_time = time.time()
        got_expected_ok = False
        got_expected_custom = False

        full_response_str = ""
        
        # Read multiple lines for the response
        while time.time() - start_time < timeout:
            if ser.in_waiting > 0:
                line_bytes = ser.readline()
                try:
                    line_str = line_bytes.decode('utf-8', errors='replace').strip()
                    if line_str: # Process non-empty lines
                        print(f"Received: {line_str}")
                        full_response_str += line_str + "\n"
                        response_lines.append(line_str)
                        if expected_ok and expected_ok in line_str:
                            got_expected_ok = True
                        if expected_custom and expected_custom in line_str:
                            got_expected_custom = True
                except Exception as e_decode:
                    print(f"Error decoding line: {line_bytes}, error: {e_decode}")
            # Break if we got both expected_ok and expected_custom (if custom is defined)
            # Or if we got expected_ok and no custom is defined
            # Or if we got expected_custom and no ok is defined (e.g. for +CSQ:)
            if expected_custom and got_expected_custom: # If a custom response is primary, check it
                break
            if expected_ok and got_expected_ok and not expected_custom : # If only OK is expected
                 break
            if not expected_ok and expected_custom and got_expected_custom: # If only custom is expected
                 break


        if expected_custom and not got_expected_custom:
             print(f"Timeout or error: Did not receive '{expected_custom}' for {command.strip()}")
        elif expected_ok and not got_expected_ok:
             print(f"Timeout or error: Did not receive '{expected_ok}' for {command.strip()}")

        return (got_expected_ok or got_expected_custom), response_lines, full_response_str.strip()

    # Initial AT command to sync
    print("--- Test 0: Initial AT sync ---")
    success, _, response_text = send_at_test("AT")
    if success: print("Initial AT command successful!\n")
    else: print(f"Initial AT command failed. Full response:\n{response_text}\n")

    # Echo off
    print("--- Test 1: Sending 'ATE0' (Echo off) ---")
    success, _, response_text = send_at_test("ATE0") # Echo off should still return OK
    if success: print("ATE0 command successful!\n")
    else: print(f"ATE0 command failed. Full response:\n{response_text}\n")

    # Wait for SIM to initialize after basic setup
    print("Waiting for SIM card to initialize (10 seconds)...")
    time.sleep(10)

    # Test 2: Check SIM card status
    print("--- Test 2: Sending 'AT+CPIN?' (Check SIM) ---")
    # For CPIN?, the response line containing "+CPIN: " is more important than just "OK"
    # The "OK" might come on a separate line after.
    success, response_lines, response_text = send_at_test("AT+CPIN?", expected_custom="+CPIN:", expected_ok="OK", timeout=5)
    if any("+CPIN: READY" in line for line in response_lines):
        print("SIM card is READY!\n")
    elif any("+CPIN: SIM PIN" in line for line in response_lines):
        print("SIM card requires PIN. Please unlock with AT+CPIN=\"YOUR_PIN\".\n")
    elif any("+CPIN:" in line for line in response_lines): # Some other +CPIN response
        print(f"SIM card status: Check response. Full response:\n{response_text}\n")
    else: # No "+CPIN:" line found, or general failure
        print(f"Could not determine SIM status or command failed. Full response:\n{response_text}\n")


    # Test 3: Check Signal Quality (only if SIM seems ready or we want to check anyway)
    print("--- Test 3: Sending 'AT+CSQ' (Signal Quality) ---")
    success, response_lines, response_text = send_at_test("AT+CSQ", expected_custom="+CSQ:", expected_ok="OK", timeout=5)
    if any("+CSQ:" in line for line in response_lines):
        print(f"Signal Quality response received. Full response:\n{response_text}\n")
    else:
        print(f"AT+CSQ command failed or no +CSQ line. Full response:\n{response_text}\n")

except serial.SerialException as e:
    print(f"Serial error: {e}")
except Exception as e:
    print(f"An general error occurred: {e}")
    import traceback
    traceback.print_exc()
finally:
    if ser and ser.is_open:
        ser.close()
        print(f"Serial port {SERIAL_PORT} closed.")