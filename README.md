# F1 Telegram Dashboard Bot

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

A comprehensive Telegram bot for Formula 1 fans, providing real-time race data, championship standings, weekend schedules, and live commentary.

This bot leverages multiple APIs to deliver a rich F1 experience directly to your Telegram chat.

<!-- Optional: Add a screenshot or GIF of the bot in action -->
<!-- ![Bot Demo GIF](link_to_your_gif.gif) -->

## Features

- **🏎️ Live Race Dashboard**: Get a real-time overview of the race, including driver positions, lap times, gaps, and current tyre compounds.
- **💬 Live Commentary**: Receive automated updates for key events like overtakes, pit stops, retirements, and official Race Control messages (yellow/red flags, penalties, etc.).
- **🏆 Championship Standings**: View the latest driver standings for the current season.
- **🗓️ Weekend Schedule**: Check the schedule for the upcoming race weekend, with session times automatically converted to your local timezone (or a timezone of your choice).
- **🏁 Last Race Results**: Quickly see the podium finishers and fastest lap from the most recently completed race.
- **📊 Historical Data**: View past winners and pole position records for the upcoming circuit.
- **▶️ Demo Mode**: See the bot in action with a simulated live race, perfect for testing or showcasing features between race weekends.

## Data Sources

This bot relies on fantastic community-driven and public APIs:

- **[OpenF1](https://openf1.org/)**: Powers the live timing, car data, and race control messages.
- **[Jolpica-F1 (Ergast-compatible)](https://github.com/jolpica/jolpica-f1-api)**: Provides reliable driver standings, historical results, and race calendar information.
- **[F1 Calendar JSON](https://github.com/sportstimes/f1)**: A community-maintained calendar used as a fallback for scheduling.

## How to Use

You can interact with the bot using simple commands or inline buttons.

### Main Commands

*   `/start` - Initializes the bot and shows the main menu.
*   `/live` - Starts the live dashboard for the current F1 session. If no session is live, it provides other options.
*   `/stop` - Stops all live updates in the chat.
*   `/standings` - Displays the current driver championship standings.
*   `/schedule` - Shows the session times for the upcoming race weekend.
*   `/commentary` - Starts the live commentary feed for events and race control messages.
*   `/help` - Shows a list of all available commands.

### Inline Buttons

The bot's messages include interactive buttons for easy navigation:

- **🔴 Start Live Updates**: Begins the live timing dashboard.
- **⏹️ Stop Updates**: Halts the live feed.
- **💬 Commentary**: Toggles the live event commentary.
- **🏆 Standings**: Fetches the latest championship standings.
- **🗓️ Weekend Schedule**: Shows the upcoming weekend's schedule.

## Running Your Own Instance

If you want to run your own version of this bot, follow these steps:

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/rufethidoaz-art/F1-telegram-dashboard.git
    cd F1-telegram-dashboard
    ```

2.  **Create a virtual environment:**
    ```bash
    python -m venv .venv
    source .venv/bin/activate  # On Windows, use `.venv\Scripts\activate`
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Create a `.env` file:**
    Create a file named `.env` in the project's root directory and add your Telegram Bot Token, which you can get from BotFather.

    ```
    TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN_HERE"
    ```

5.  **Run the bot:**
    ```bash
    python main.py
    ```

## License

This project is open-source and available under the MIT License.