# OBSOLETE: Manual legacy entry point. Use main.py / core.command_handler instead.
from core.notifier import notify_start


def main():

    notify_start()

    print("PrimeBot Control Bot Online")


if __name__ == "__main__":
    main()