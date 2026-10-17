/*  Single-lead ECG sampler — AD8232 -> USB serial (ASCII)
 *  ---------------------------------------------------------------
 *  Reference firmware for the hardware described in ../README.md.
 *  Wire format expected by the recorder (web plotter / Android app):
 *
 *      9600 baud, 8N1, one integer per line, "\r\n", range 0..1023
 *
 *  ~192 samples/s is the ceiling at 9600 baud (960 bytes/s, 5 bytes
 *  per line, e.g. "512\r\n"). The sampler therefore self-paces from
 *  the UART: the loop produces a sample only when the TX buffer can
 *  take it, so the effective rate settles just below 192 Hz instead
 *  of producing gaps. Consumers timestamp every sample by arrival
 *  time, so a small rate difference is harmless — but the recorder
 *  must be told the same nominal rate (UI parameter, default 192).
 *
 *  Board: any AVR/ESP32 board with A0..A5 analog inputs (tested shape
 *  of the protocol: Arduino Nano/Uno class, CH340 USB-serial).
 *  Safety: battery power only while electrodes are on the body.
 *  This is NOT a medical device and must not be used on other people.
 */

const uint8_t ECG_PIN   = A0;   // AD8232 OUTPUT
const uint8_t LO_PLUS   = 10;   // AD8232 LO+  (leads-off, optional)
const uint8_t LO_MINUS  = 11;   // AD8232 LO-  (leads-off, optional)

const unsigned long BAUD      = 9600;
const unsigned int  TARGET_HZ = 192;
const unsigned long PERIOD_US = 1000000UL / TARGET_HZ;   // 5208 us

unsigned long next_us = 0;

void setup() {
  Serial.begin(BAUD);
  pinMode(LO_PLUS, INPUT);
  pinMode(LO_MINUS, INPUT);
#if defined(__AVR__)
  // Fast ADC: prescaler 16 -> 76.9 kHz ADC clock, ~13 kSPS. Default
  // 125 kHz (~9.6 kSPS) is also fine for 192 Hz, so this is optional.
  ADCSRA = (ADCSRA & 0xF8) | 0x04;
#endif
  next_us = micros();
}

void loop() {
  // 1) pace the sampler
  unsigned long now = micros();
  if ((long)(now - next_us) < 0) return;
  next_us += PERIOD_US;
  if ((long)(now - next_us) > 20000) next_us = now;   // resync after a stall

  // 2) one ADC conversion
  int v = analogRead(ECG_PIN);

  // 3) one line, only if the UART can take it (keeps the rate honest)
#if defined(ARDUINO_ARCH_AVR) || defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
  if (Serial.availableForWrite() < 6) return;
#endif
  Serial.print(v);
  Serial.print("\r\n");
}
