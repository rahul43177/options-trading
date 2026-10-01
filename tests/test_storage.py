import tempfile,unittest
from pathlib import Path
from research.storage import connect,store_chain
class StorageTests(unittest.TestCase):
 def test_idempotent_snapshot(self):
  with tempfile.TemporaryDirectory() as d:
   c=connect(Path(d)/'x.sqlite'); x={'symbol':'C-BTC-100-010101','product_id':1,'strike_price':'100','contract_type':'call_options','spot_price':'100','mark_price':'2','quotes':{},'greeks':{}}
   store_chain(c,[x],1);store_chain(c,[x],1);self.assertEqual(c.execute('select count(*) from option_snapshots').fetchone()[0],1)
if __name__ == '__main__': unittest.main()
