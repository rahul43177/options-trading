import unittest
from research.pricing import delta,price

class PricingTests(unittest.TestCase):
    def test_expiry_intrinsic(self):
        self.assertEqual(price(110,100,0,.5,'call'),10)
        self.assertEqual(price(90,100,0,.5,'put'),10)
    def test_put_call_parity_at_zero_rate(self):
        call=price(100,100,30/365,.5,'call'); put=price(100,100,30/365,.5,'put')
        self.assertAlmostEqual(call,put,places=8)
    def test_delta_signs(self):
        self.assertGreater(delta(100,100,30/365,.5,'call'),0)
        self.assertLess(delta(100,100,30/365,.5,'put'),0)
if __name__ == '__main__': unittest.main()
