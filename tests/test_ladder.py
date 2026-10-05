import unittest

from research.ladder import parse, validate

GOOD = ("TS=2026-10-04 18:23 UTC;STATUS=ARMED;EXP=06-Oct-2026;NR=85467-85586;SR=86055-86100;"
        "NS=84619-85291;SS=83830-84415;TRIG=85000-85250;BEST=P83200@89;"
        "ALT=P83800@162|C87200@164;INV=85000|84620|83650;MP=85200")
SPOT = 85319.0


class ParseTests(unittest.TestCase):
    def test_parses_every_key(self):
        p = parse(GOOD)
        self.assertEqual(p["STATUS"], "ARMED")
        self.assertEqual(p["NS"], (84619.0, 85291.0))
        self.assertEqual(p["SR"], (86055.0, 86100.0))
        self.assertEqual(p["BEST"], {"side": "P", "strike": 83200.0, "rest": "89"})
        self.assertEqual([a["strike"] for a in p["ALT"]], [83800.0, 87200.0])
        self.assertEqual(p["INV"], [85000.0, 84620.0, 83650.0])
        self.assertEqual(p["MP"], 85200.0)

    def test_commas_spaces_newlines_single_value_band(self):
        p = parse("SR = 86,100\nNS=84,619 - 85,291 ; best = p 83,200 @ 89")
        self.assertEqual(p["SR"], (86100.0, 86100.0))
        self.assertEqual(p["NS"], (84619.0, 85291.0))
        self.assertEqual(p["BEST"]["strike"], 83200.0)


class ValidateTests(unittest.TestCase):
    def test_good_line_has_no_errors(self):
        errors, _ = validate(GOOD, SPOT)
        self.assertEqual(errors, [])

    def test_resistance_below_spot_is_an_error(self):
        # the exact mistake from the un-pinned zones run: call arrival 85,300 under spot 85,319
        errors, _ = validate(GOOD.replace("NR=85467-85586", "NR=85291-85586"), SPOT)
        self.assertTrue(any("NR" in e and "not above spot" in e for e in errors))

    def test_put_strike_inside_trigger_is_an_error(self):
        errors, _ = validate(GOOD.replace("BEST=P83200@89", "BEST=P85100@300"), SPOT)
        self.assertTrue(any("trigger zone" in e for e in errors))

    def test_invalidation_on_wrong_side_and_unknown_key(self):
        errors, _ = validate(GOOD.replace("INV=85000|84620|83650", "INV=85600") + ";FOO=1", SPOT)
        self.assertTrue(any("INV 85,600" in e for e in errors))
        self.assertIn("unknown key FOO", errors)

    def test_missing_required_and_bad_status(self):
        errors, _ = validate("STATUS=MAYBE;NS=84000-85000", SPOT)
        self.assertIn("missing TS", errors)
        self.assertIn("missing EXP", errors)
        self.assertTrue(any("STATUS must be" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
