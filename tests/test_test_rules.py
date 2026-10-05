"""Rules for the tests themselves, checked in code so that a lesson cannot come back.

A page test must move prices from the page's state, never on a wall clock. Timers raced the page load three times
(T2 TEST-PORT-CONTENTION, T5 TEST-FOCUS-STATIC-MARK, T5 TEST-PL-MOVE-TIMER-RACE). The look step {"signal": name}
pauses the page at a known state; the test then sends the price in on={name: fn}, and the page waits for the change.
"""
import ast
from pathlib import Path

PAGE_TESTS = Path(__file__).parent / "test_pages.py"

# The calls that run code on a wall clock, beside the page.
WALL_CLOCK = {"Timer", "sleep", "Thread"}

# The only allowed ones: in the harness, never in a test.
ALLOWED = {("Tool.__init__", "Thread"): "the app's server thread",
           ("Tool.__init__", "sleep"): "the wait for the server to start",
           ("Tool.look", "Timer"): "the 120 s safety kill of look.mjs"}


def wall_clock_moves(source):
    """Each wall-clock call in source that ALLOWED does not name, as 'line N, function: call ...'."""
    found = []

    def walk(node, where):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, where + [child.name])
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in WALL_CLOCK and (".".join(where[:2]), name) not in ALLOWED:
                    found.append(f"line {child.lineno}, {'.'.join(where) or 'module'}: {ast.unparse(f)} runs on a wall clock; "
                                 'send the price at a look step {"signal": name} with on={name: fn}')
            walk(child, where)
    walk(ast.parse(source), [])
    return found


def test_page_tests_move_prices_only_at_a_signal_step():
    assert wall_clock_moves(PAGE_TESTS.read_text()) == []


# The pattern of T5 TEST-PL-MOVE-TIMER-RACE, as it was in tests/test_pages.py at 9a825a1.
OLD_TIMER = '''
def test_page_the_close_list_profit_follows_the_mark(tool):
    def move():
        tool.book("BTC/USD", [("62000.0", "0"), ("61900.0", "1")], [("62000.6", "0"), ("61900.6", "3")])
    th = threading.Timer(2.0, move)
    th.start()
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "+12.83"}])
    th.join()
'''

# A price loop beside the page, as the focus test had it before T40.
OLD_LOOP = '''
def test_page_keeps_the_focus(tool):
    def prices():
        while not stop.is_set():
            tool.book("BTC/USD", [("61900.0", "1")], [])
            time.sleep(0.15)
    th = threading.Thread(target=prices, daemon=True)
'''


def test_the_rule_names_the_old_timer_and_the_signal_step_to_use():
    assert wall_clock_moves(OLD_TIMER) == [
        'line 5, test_page_the_close_list_profit_follows_the_mark: threading.Timer runs on a wall clock; '
        'send the price at a look step {"signal": name} with on={name: fn}']


def test_the_rule_finds_a_price_loop_with_a_sleep_in_a_nested_function():
    assert [m.split(":")[0] for m in wall_clock_moves(OLD_LOOP)] == [
        "line 6, test_page_keeps_the_focus.prices", "line 7, test_page_keeps_the_focus"]


def test_the_rule_allows_the_safety_kill_only_in_tool_look():
    kill = "        kill = threading.Timer(120, p.kill)\n"
    assert wall_clock_moves("class Tool:\n    def look(self):\n" + kill) == []
    assert len(wall_clock_moves("class Tool:\n    def other(self):\n" + kill)) == 1
    assert len(wall_clock_moves("def test_page_x(tool):\n" + kill)) == 1
