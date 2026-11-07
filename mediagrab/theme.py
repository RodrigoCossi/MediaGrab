"""Dark theme. Colours are tokens; the accent changes per product."""

BG = "#121418"
PANEL = "#1a1d23"
CARD = "#1f232b"
CARD_HI = "#262b35"
BORDER = "#2d323d"
TEXT = "#e9ecf2"
MUTED = "#98a1b3"
FAINT = "#6b7385"
OK = "#34d399"
ERR = "#f87171"
WARN = "#fbbf24"


def qss(accent: str, accent_hi: str) -> str:
    return f"""
* {{ outline: none; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QToolTip {{ background: {CARD_HI}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 8px; }}
#TopBar, #Footer {{ background: {PANEL}; }}
#TopBar {{ border-bottom: 1px solid {BORDER}; }}
#Footer {{ border-top: 1px solid {BORDER}; }}
#TopBar QLabel, #Footer QLabel, #Footer QWidget {{ background: transparent; }}
#Brand {{ font-size: 13pt; font-weight: 700; }}
#BrandSub {{ color: {MUTED}; font-size: 9pt; }}
#Muted {{ color: {MUTED}; }}
#Faint {{ color: {FAINT}; }}
#SectionTitle {{ color: {MUTED}; font-size: 9pt; font-weight: 700; letter-spacing: 1px; }}
#InputCard {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
#InputCard QLabel {{ background: transparent; }}

QLineEdit, QComboBox, QSpinBox {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 11px;
    selection-background-color: {accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {accent}; }}
#UrlEdit {{ font-size: 11pt; padding: 10px 14px; border-radius: 11px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER};
    selection-background-color: {CARD_HI}; padding: 4px; }}

QPushButton {{ background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 8px 16px; font-weight: 600; }}
QPushButton:hover {{ background: #2e3440; }}
QPushButton:pressed {{ background: {CARD}; }}
QPushButton:disabled {{ color: {FAINT}; }}
QPushButton#Accent {{ background: {accent}; border: 1px solid {accent}; color: #ffffff; }}
QPushButton#Accent:hover {{ background: {accent_hi}; border-color: {accent_hi}; }}
QPushButton#Accent:disabled {{ background: {CARD_HI}; border-color: {BORDER}; color: {FAINT}; }}
QPushButton#Big {{ padding: 10px 20px; font-size: 11pt; border-radius: 11px; }}
QPushButton#Toggle {{ background: transparent; border: 1px solid {BORDER}; color: {MUTED}; }}
QPushButton#Toggle:checked {{ border-color: {accent}; color: {TEXT}; background: rgba(255,255,255,0.04); }}
QPushButton#Link {{ background: transparent; border: none; color: {MUTED}; padding: 4px 8px; font-weight: 500; }}
QPushButton#Link:hover {{ color: {TEXT}; }}

QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton:hover {{ background: #323846; }}
QToolButton:pressed {{ background: {CARD}; }}

QScrollArea, #ListHost {{ border: none; background: {BG}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3a4050; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #4a5164; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}

#Row {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
#Row[hover="true"] {{ background: {CARD_HI}; }}
#Row QLabel, #Row QWidget#RowMid {{ background: transparent; }}
#RowTitle {{ font-weight: 600; font-size: 10.5pt; }}
#Thumb {{ background: #0c0e12; border-radius: 8px; }}
#Badge {{ background: rgba(255,255,255,0.07); color: {MUTED}; border-radius: 6px; padding: 1px 7px;
    font-size: 8.5pt; font-weight: 700; }}

QProgressBar {{ background: #0f1115; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}
QProgressBar[state="paused"]::chunk {{ background: {FAINT}; }}

QDialog {{ background: {PANEL}; }}
QDialog QLabel, QDialog QCheckBox, QDialog QRadioButton {{ background: transparent; }}
QCheckBox, QRadioButton {{ spacing: 9px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; border: 1px solid #4a5164;
    background: {BG}; }}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QCheckBox::indicator:checked {{ image: url(:/check); }}
#Option {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; padding: 9px 12px; }}
#Option:hover {{ background: {CARD_HI}; }}
#Option:checked {{ border-color: {accent}; background: {CARD_HI}; }}
QListWidget {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 10px; padding: 4px; }}
QListWidget::item {{ padding: 6px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {CARD}; }}
QListWidget::item:selected {{ background: {CARD_HI}; color: {TEXT}; }}
QListWidget::indicator {{ width: 16px; height: 16px; border: 1px solid #4a5164; border-radius: 4px; background: {BG}; }}
QListWidget::indicator:checked {{ background: {accent}; border-color: {accent}; image: url(:/check); }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; background: {PANEL}; }}
QTabBar {{ background: transparent; }}
QTabWidget {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 18px; border: none;
    border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {accent}; }}
QTabWidget QWidget#TabPage {{ background: {PANEL}; }}
QTabWidget QWidget#TabPage QLabel, QTabWidget QWidget#TabPage QCheckBox {{ background: transparent; }}
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {CARD_HI}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
"""
