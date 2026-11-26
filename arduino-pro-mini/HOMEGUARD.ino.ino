#include "Sensors.h"
#include "Communication.h"

#include <avr/sleep.h>
#include <avr/power.h>
#include <avr/interrupt.h>

#define LED_PIN 4
#define RELAY_PIN 5
#define BUZZER_PIN 6
#define WAKE_PIN 2

// Change if relay module is active HIGH.
#define RELAY_ON LOW
#define RELAY_OFF HIGH

// Initial demonstration thresholds
#define GAS_THRESHOLD 300
#define TEMP_THRESHOLD 35.0
#define DISTANCE_THRESHOLD 20.0

enum Mode {
  AUTO,
  MAINTENANCE,
  SLEEP
};

Mode currentMode = AUTO;

Sensors sensors;
Communication comm;

unsigned long lastReading = 0;
const unsigned long readInterval = 2000;

bool fanState = false;
bool buzzerState = false;
bool ledState = false;

volatile bool wakeRequested = false;

// Hardware interrupt
void wakeISR() {
  wakeRequested = true;
  detachInterrupt(digitalPinToInterrupt(WAKE_PIN));
}

void updateOutputs() {
  digitalWrite(
    RELAY_PIN,
    fanState ? RELAY_ON : RELAY_OFF
  );

  digitalWrite(BUZZER_PIN, buzzerState);
  digitalWrite(LED_PIN, ledState);
}

void turnOffOutputs() {
  fanState = false;
  buzzerState = false;
  ledState = false;

  updateOutputs();
}

void disableUnusedPins() {
  const uint8_t unusedPins[] = {
    3, 10, 11, 12, 13,
    A1, A2, A3, A4, A5
  };

  for (uint8_t pin : unusedPins) {
    pinMode(pin, INPUT_PULLUP);
  }
}

void automaticOperation() {
  bool highGas =
    sensors.gas >= GAS_THRESHOLD;

  bool highTemperature =
    !isnan(sensors.temperature) &&
    sensors.temperature >= TEMP_THRESHOLD;

  bool nearbyObject =
    !isnan(sensors.distance) &&
    sensors.distance <= DISTANCE_THRESHOLD;

  fanState = highGas || highTemperature;
  buzzerState = highGas || nearbyObject;
  ledState = fanState || buzzerState;

  updateOutputs();
}

void sendReadings() {
  sensors.readAll();

  if (currentMode == AUTO) {
    automaticOperation();
  }

  comm.sendData(
    sensors,
    fanState,
    buzzerState,
    ledState
  );
}

void enterSleep() {
  turnOffOutputs();

  comm.sendStatus("SLEEPING");
  Serial.flush();

  wakeRequested = false;

  ADCSRA &= ~_BV(ADEN);

  set_sleep_mode(SLEEP_MODE_PWR_DOWN);

  noInterrupts();

  attachInterrupt(
    digitalPinToInterrupt(WAKE_PIN),
    wakeISR,
    LOW
  );

  if (digitalRead(WAKE_PIN) == HIGH) {
    sleep_enable();

    // Disable brown-out detection.
    sleep_bod_disable();

    interrupts();
    sleep_cpu();

    sleep_disable();
  }

  interrupts();

  detachInterrupt(
    digitalPinToInterrupt(WAKE_PIN)
  );

  ADCSRA |= _BV(ADEN);

  currentMode = AUTO;

  comm.sendStatus("AWAKE");
  comm.sendStatus("AUTO");

  lastReading = millis();
}

void handleCommand(String command) {
  if (command == "AUTO") {
    currentMode = AUTO;
    comm.sendStatus("AUTO");
  }

  else if (command == "MAINTENANCE") {
    currentMode = MAINTENANCE;
    turnOffOutputs();

    comm.sendStatus("MAINTENANCE");
  }

  else if (command == "SLEEP") {
    currentMode = SLEEP;
  }

  else if (currentMode == MAINTENANCE) {

    if (command == "READ") {
      sendReadings();
    }

    else if (command == "FAN_ON") {
      fanState = true;
    }

    else if (command == "FAN_OFF") {
      fanState = false;
    }

    else if (command == "BUZZER_ON") {
      buzzerState = true;
    }

    else if (command == "BUZZER_OFF") {
      buzzerState = false;
    }

    else if (command == "LED_ON") {
      ledState = true;
    }

    else if (command == "LED_OFF") {
      ledState = false;
    }

    updateOutputs();

    comm.sendData(
      sensors,
      fanState,
      buzzerState,
      ledState
    );
  }
}

void setup() {
  pinMode(LED_PIN, OUTPUT);

  digitalWrite(RELAY_PIN, RELAY_OFF);
  pinMode(RELAY_PIN, OUTPUT);

  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(WAKE_PIN, INPUT_PULLUP);

  turnOffOutputs();
  disableUnusedPins();

  sensors.begin();
  comm.begin();

  comm.sendStatus("HOMEGUARD_READY");
  comm.sendStatus("AUTO");
}

void loop() {
  String command = comm.receive();

  if (command.length() > 0) {
    handleCommand(command);
  }

  switch (currentMode) {

    case AUTO:
      if (millis() - lastReading >= readInterval) {
        lastReading = millis();
        sendReadings();
      }
      break;

    case MAINTENANCE:
      // Automatic operation is paused.
      break;

    case SLEEP:
      enterSleep();
      break;
  }
}