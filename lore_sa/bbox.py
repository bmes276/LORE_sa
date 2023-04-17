from abc import ABC, abstractmethod

class AbstractBBox(ABC):
    """
    Generic Black Box class witch provides two sklearn-like methods.

    pass
    """

    def __init__(self):
        pass

    @abstractmethod
    def predict(self, sample_matrix: list):
        """
        Wrap of sklearn predict method, that predict the class labels for the provided data.

        :param sample_matrix: {array-like, sparse matrix} of shape (n_queries, n_features) samples.
        :return: ndarray of shape (n_queries, n_classes), or a list of n_outputs of such arrays if n_outputs > 1.

        """
        pass

    @abstractmethod
    def predict_proba(self, sample_matrix: list):
        """
        Wrap of sklearn predict_proba method, that return probability estimates for the test data.

        :param sample_matrix: {array-like, sparse matrix} of shape (n_queries, n_features) samples
        :return: ndarray of shape (n_queries, n_classes), or a list of n_outputs of such arrays if n_outputs > 1.

        """
        pass