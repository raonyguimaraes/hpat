import h5py
import numpy as np
import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd


def gen_lr(file_name, N, D):
    points = np.random.random((N,D))
    responses = np.random.random(N)
    f = h5py.File(file_name, "w")
    dset1 = f.create_dataset("points", (N,D), dtype='f8')
    dset1[:] = points
    dset2 = f.create_dataset("responses", (N,), dtype='f8')
    dset2[:] = responses
    f.close()

def gen_kde_pq(file_name, N):
    df = pd.DataFrame({'points': np.random.random(N)})
    table = pa.Table.from_pandas(df)
    row_group_size = 128
    pq.write_table(table, file_name, row_group_size)

def gen_pq_test(file_name):
    df = pd.DataFrame({'one': [-1, np.nan, 2.5, 3., 4., 6.],
                           'two': ['foo', 'bar', 'baz', 'foo', 'bar', 'baz'],
                           'three': [True, False, True, True, True, False]})
    table = pa.Table.from_pandas(df)
    pq.write_table(table, 'example.parquet')

N = 101
D = 10
gen_lr("lr.hdf5", N, D)
gen_kde_pq('kde.parquet', N)
gen_pq_test('example.parquet')

df = pd.DataFrame({'A': ['bc']+["a"]*3+ ["bc"]*3+['a'], 'B': [-8,1,2,3,1,5,6,7]})
df.to_parquet("groupby3.pq")

df = pd.DataFrame({"A": ["foo", "foo", "foo", "foo", "foo",
                          "bar", "bar", "bar", "bar"],
                    "B": ["one", "one", "one", "two", "two",
                          "one", "one", "two", "two"],
                    "C": ["small", "large", "large", "small",
                          "small", "large", "small", "small",
                          "large"],
                    "D": [1, 2, 2, 6, 3, 4, 5, 6, 9]})
df.to_parquet("pivot2.pq")
