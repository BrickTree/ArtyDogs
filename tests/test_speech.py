import unittest

from arty.speech import Speaker, digits


class Wording(unittest.TestCase):
    def test_radio_style_digits(self):
        self.assertEqual(digits("090.0"), "zero niner zero point zero")
        self.assertEqual(digits("1219"), "one two one niner")
        self.assertEqual(digits("m-5"), "minus five")  # anything that isn't a digit is dropped

    def test_settings_are_clamped(self):
        s = Speaker(volume=150, rate=-40)  # not started: no process, nothing spoken
        self.assertEqual((s.volume, s.rate), (100, -10))
        s.set_volume(-5)
        self.assertEqual(s.volume, 0)

    def test_silent_at_zero_volume(self):
        s = Speaker(volume=0)
        s.say("anything")
        self.assertTrue(s._commands.empty())


if __name__ == "__main__":
    unittest.main()
