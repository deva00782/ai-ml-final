import serial
from agents.uart_server import find_uart_port

class SerialReceiverAgent:
    def __init__(self, port="AUTO", baud=115200):
        if not port or str(port).upper() in ("AUTO", "NONE"):
            adapter = find_uart_port()
            if adapter:
                port = adapter["port"]
            else:
                raise serial.SerialException("No USB-to-UART adapter found! Connect adapter or specify port.")
        self.ser = serial.Serial(port, baud, timeout=1)

    def read_line(self):
        """Reads and cleans raw string incoming over serial."""
        if self.ser.in_waiting > 0:
            return self.ser.readline().decode('utf-8', errors='ignore').strip()
        return None

    def send_ack(self, ack_message):
        """Sends ACK packet back to PC1."""
        self.ser.write(ack_message.encode('utf-8'))

    def close(self):
        if self.ser.is_open:
            self.ser.close()