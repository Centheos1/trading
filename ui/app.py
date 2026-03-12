#!/usr/bin/env python3
"""Order Flow Trading UI - Bookmap-style visualization."""

import sys
import os
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from ui.main_window import MainWindow

logger = logging.getLogger()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s"
    )

    app = QApplication(sys.argv)
    app.setApplicationName("OrderFlow Trading")
    app.setFont(QFont("Menlo", 10))

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
