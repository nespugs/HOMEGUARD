# HOMEGUARD
### An Arduino Pro Mini and ESP-01-Based Embedded Monitoring and Security System

## Project Overview
HOMEGUARD is an embedded monitoring and security system designed to monitor environmental conditions and detect potential safety concerns. It integrates sensors, automated alerts, wireless communication, a web interface, and database storage.

## Hardware Components
- Arduino Pro Mini (ATmega328P)
- ESP-01 (ESP8266)
- DHT11 Temperature and Humidity Sensor
- MQ-2 Gas and Smoke Sensor
- HC-SR04 Ultrasonic Sensor
- LED Indicator
- Active Buzzer
- Relay Module
- 12V DC Fan

## System Features
- Temperature and humidity monitoring
- Gas and smoke detection
- Proximity-based intrusion alerts
- Automatic fan and buzzer activation
- Web-based monitoring dashboard
- Sensor logs and alert history
- Local MySQL database storage
- Supabase cloud synchronization
- Low-power sleep and wake-up implementation

**Development Note:** Some features, including website-controlled hardware outputs and ESP-01 offline buffering, are still undergoing integration and testing.

## Repository Structure

```text
HOMEGUARD/
├── arduino-pro-mini/
├── backend/
├── esp01/
├── website/
└── README.md
```

## Software Technologies
- Arduino IDE / C++
- ESP8266 Wi-Fi
- Python / FastAPI
- HTML, CSS, JavaScript
- MySQL
- Supabase
- Cloudflare Tunnel

## Project Documentation
This repository contains the source code and related files developed for the HOMEGUARD embedded systems project.

## Project Status
Prototype development and testing.

