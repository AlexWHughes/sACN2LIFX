# sACN2LIFX

Control LIFX lights via sACN/E1.31 with automatic discovery and web-based mapping interface.

## Features

- **Automatic LIFX Discovery**: Automatically discovers all LIFX lights on your network
- **Web UI**: Simple web interface for mapping lights to DMX universes and channels
- **E1.31 Support**: Receives DMX data via sACN (E1.31) protocol
- **Persistent Mappings**: Saves your light mappings to disk
- **Brightness Control**: Per-light brightness adjustment

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

1. Start the server:
```bash
python app.py
```

2. Open your browser to `http://localhost:5001`

3. Click "Discover Lights" to find all LIFX devices on your network

4. For each light, configure:
   - **Universe**: The DMX universe number (typically 1-512)
   - **Start Channel**: The first DMX channel for this light (RGB uses 3 channels)
   - **Brightness**: Overall brightness multiplier (0.0 - 1.0)

5. Click "Start DMX" to begin processing DMX data

## DMX Channel Mapping

Each light uses 3 consecutive DMX channels:
- Channel N: Red (0-255)
- Channel N+1: Green (0-255)
- Channel N+2: Blue (0-255)

For example, if a light is mapped to Universe 1, Channel 1:
- Channel 1 = Red
- Channel 2 = Green
- Channel 3 = Blue

## Configuration

Mappings and settings are automatically saved to `config.json` in the project directory.

## Requirements

- Python 3.7+
- LIFX lights on the same network
- DMX/E1.31 source (e.g., lighting console, software)

