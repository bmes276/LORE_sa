from lore_sa.dataset import Dataset

from lore_sa.logger import logger
import pandas as pd
from pandas import DataFrame
import numpy as np

__all__ = ["TabularDataset","Dataset"]


class TabularDataset(Dataset):
    """
    It provides an interface to handle datasets, including some essential information on the structure and
    semantic of the dataset.

    Attributes:
        df (pandas.DataFrame): dataframe containing the whole dataset
        descriptor (dict): it contains the essential informationregarding each feature. Format:

            >>> {'numeric': {'feature name' :
                            {
                                'index' : <index of feature column>,
                                'min' : <min value>,
                                'max' : <max value>,
                                'mean': <mean value>,
                                'std': <standard deviation>,
                                'median': <median value>,
                                'q1': <first quartile of the distribution>,
                                'q3': <third quartile of the distribution,
                            },
                        ...,
                        ...,
                        },
            'categorical: {'feature name':
                                {
                                    'index' : <index of feature column>,
                                    'distinct_values' : <distinct categorical values>,
                                    'value_counts' : {'distinct value' : <elements count>,
                                                    ... }
                                }
                            },
                            ...
                            ...
                            ...     
            }
    """
    def __init__(self,data: DataFrame, class_name:str = None):
        
        self.class_name = class_name
        self.df = data

        #target columns forced to be the last column of the dataset
        if class_name is not None:
            self.df = self.df[[x for x in self.df.columns if x != class_name] + [class_name]]
        
        self.descriptor = {'numeric':{}, 'categorical':{}}

        #creation of a default version of descriptor
        self.update_descriptor()
        
    def update_descriptor(self):
        """
        it creates the dataset descriptor dictionary
        """
        self.descriptor = {'numeric':{}, 'categorical':{}}
        for feature in self.df.columns:
            index = self.df.columns.get_loc(feature)
            if feature in self.df.select_dtypes(include=np.number).columns.tolist():
                #numerical
                desc = {'index': index,
                        'min' : self.df[feature].min(),
                        'max' : self.df[feature].max(),
                        'mean':self.df[feature].mean(),
                        'std':self.df[feature].std(),
                        'median':self.df[feature].median(),
                        'q1':self.df[feature].quantile(0.25),
                        'q3':self.df[feature].quantile(0.75),
                        }
                self.descriptor['numeric'][feature] = desc
            else:
                #categorical feature
                desc = {'index': index,
                        'distinct_values' : list(self.df[feature].unique()),
                        'count' : {x : len(self.df[self.df[feature] == x]) for x in list(self.df[feature].unique())}}
                self.descriptor['categorical'][feature] = desc

        self.descriptor = self.set_target_label(self.descriptor)

    def set_target_label(self, descriptor):
        """
        Set the target column into the dataset descriptor

        :param descriptor:
        :return:
        """
        if self.class_name is None:
            logger.warning("No target class is defined")
            return descriptor

        for type in descriptor:
            for k in descriptor[type]:
                if k == self.class_name:
                    descriptor['target'] = {k:descriptor[type][k]}
                    descriptor[type].pop(k)
                    return descriptor
                
        return descriptor
        
    
    @classmethod
    def from_csv(cls, filename: str, class_name: str=None):
        """
        Read a comma-separated values (csv) file into Dataset object.
        :param [str] filename:
        :param class_name: optional
        :return:
        """
        df = pd.read_csv(filename, skipinitialspace=True, na_values='?', keep_default_na=True)
        dataset_obj = cls(df, class_name=class_name)
        dataset_obj.filename = filename
        logger.info('{0} file imported'.format(filename))
        return dataset_obj

    @classmethod
    def from_dict(cls, data: dict, class_name: str=None):
        """
        From dicts of Series, arrays, or dicts.
        :param [dict] data:
        :param class_name: optional
        :return:
        """
        return cls(pd.DataFrame(data), class_name=class_name)

    def set_class_name(self,class_name: str):
        """
        Set the class name. Only the column name string
        :param [str] class_name:
        :return:
        """
        self.class_name = class_name

    def get_class_values(self):
        """
        return the list of values of the target column
        :return:
        """
        if self.class_name is None:
            raise Exception("ERR: class_name is None. Set class_name with set_class_name('<column name>')")
        return self.descriptor['target'][self.class_name]['distinct_values']


    def get_numeric_columns(self):
        numeric_columns = list(self.df._get_numeric_data().columns)
        return numeric_columns

    def get_features_names(self):
        return list(self.df.columns)

