import unittest
from photo_triage.faces import matches,vector


class FaceMatchTests(unittest.TestCase):
    def test_same_and_different_embeddings(self):
        a=[1.]+[0.]*127;b=[0.,1.]+[0.]*126
        self.assertTrue(matches([a],[a])['matched'])
        self.assertFalse(matches([b],[a])['matched'])
        self.assertTrue(matches([b,a],[a])['matched'])
        self.assertFalse(matches([],[a])['matched'])

    def test_invalid_vectors_fail(self):
        for bad in ([0.]*128,[1.]*127,[float('nan')]*128):
            with self.assertRaises(ValueError):vector(bad)
