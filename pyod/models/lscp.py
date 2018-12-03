"""
Locally Selective Combination of Parallel Outlier Ensembles (LSCP)
Adapted from the original implementation:
"""
# Author: Zain Nasrullah
# License: BSD 2 clause

# system imports
import collections
import warnings

# numpy
import numpy as np

# sklearn imports
from sklearn.neighbors import KDTree
from sklearn.utils import check_array
from sklearn.utils.validation import check_is_fitted
from sklearn.utils.validation import check_random_state

# PYOD imports
from pyod.models.base import BaseDetector
from pyod.utils.stat_models import pearsonr
from pyod.utils.utility import argmaxn
from pyod.utils.utility import generate_bagging_indices
from pyod.utils.utility import standardizer
from pyod.utils.utility import check_detector


class LSCP(BaseDetector):
    """ Locally Selection Combination in Parallel Outlier Ensembles

    LSCP is an unsupervised parallel outlier detection ensemble which selects
    competent detectors in the local region of a test instance. This implementation
    uses an Average of Maximum strategy. First, a heterogeneous list of base detectors
    is fit to the training data and then generates a pseudo ground truth for each train instance
    is generated by taking the maximum outlier score.

    For each test instance:
    1) The local region is defined to be the set of nearest training points in randomly
    sampled feature subspaces which occur more frequently than a defined threshold
    over multiple iterations.

    2) Using the local region, a local pseudo ground truth is defined and the
    pearson correlation is calculated between each base detector's training outlier
    scores and the pseudo ground truth.

    3) A histogram is built out of pearson correlation scores; detectors in the largest bin
    are selected as competent base detectors for the given test instance.

    4) The average outlier score of the selected competent detectors is taken to be the final score.

    Parameters
    ----------
    estimator_list : List, length must be greater than 1
        Base unsupervised outlier detectors from PyOD. (Note: requires fit and decision_function methods)
    local_region_size : int, optional (default=30)
        Number of training points to consider in each iteration of the local region generation process (30 by default).
    local_max_features : float in (0.5, 1.), optional (default=1.0)
        Maximum proportion of number of features to consider when defining the local region (1.0 by default).
    n_bins : int, optional (default=10)
        Number of bins to use when selecting the local region
    random_state : RandomState, optional
        A random number generator instance to define the state of the random permutations generator.
    contamination : float in (0., 0.5), optional (default=0.1)
        The amount of contamination of the data set, i.e.
        the proportion of outliers in the data set. Used when fitting to
        define the threshold on the decision function (0.1 by default).

    Attributes
    ----------
    decision_scores_ : numpy array of shape (n_samples,)
        The outlier scores of the training data.
        The higher, the more abnormal. Outliers tend to have higher
        scores. This value is available once the detector is
        fitted.

    threshold_ : float
        The threshold is based on ``contamination``. It is the
        ``n_samples * contamination`` most abnormal samples in
        ``decision_scores_``. The threshold is calculated for generating
        binary outlier labels.

    labels_ : int, either 0 or 1
        The binary labels of the training data. 0 stands for inliers
        and 1 for outliers/anomalies. It is generated by applying
        ``threshold_`` on ``decision_scores_``.

    Examples
    --------
    >>> from pyod.utils.data import generate_data
    >>> from pyod.utils.utility import standardizer
    >>> from pyod.models.lscp import LSCP
    >>> from pyod.models.lof import LOF

    >>> X_train, y_train, X_test, y_test = generate_data(
            n_train=50, n_test=50,
            contamination=0.1, random_state=42)
    >>> X_train, X_test = standardizer(X_train, X_test)
    >>> estimator_list = [LOF(), LOF()]
    >>> clf = LSCP(estimator_list)
    >>> clf.fit(X_train)
    >>> print(clf.decision_scores_)


    """

    def __init__(self, estimator_list, local_region_size=30, local_max_features=1.0, n_bins=10,
                 random_state=None, contamination=0.1):
        super(LSCP, self).__init__(contamination=contamination)
        self.estimator_list = estimator_list
        self.n_clf = len(self.estimator_list)
        self.local_region_size = local_region_size
        self.local_region_min = 30
        self.local_region_max = 200
        self.local_max_features = local_max_features
        self.local_min_features = 0.5
        self.local_region_iterations = 20
        self.local_region_threshold = int(self.local_region_iterations / 2)
        self.n_bins = n_bins
        self.n_selected = 1
        self.random_state = random_state

        assert len(estimator_list) > 1, "The estimator list has less than 2 estimators."

        if self.local_max_features > 1.0:
            warnings.warn("Local max features greater than 1.0, reducing to 1.0")
            self.local_max_features = 1.0

        for estimator in self.estimator_list:
            check_detector(estimator)

    def fit(self, X, y=None):
        """ Fit LSCP using X as training data

        Parameters
        ----------
        X : numpy array, shape (n_samples, n_features)
            Training data
        y : None, optional (default=None)
            Labels not necessary for unsupervised method

        Returns
        -------
        None
        """
        self.random_state = check_random_state(self.random_state)
        X = check_array(X)
        self._set_n_classes(y)
        self.n_features_ = X.shape[1]

        # normalize input data
        self.X_train_norm_ = X
        train_scores = np.zeros([self.X_train_norm_.shape[0], self.n_clf])

        # fit each base estimator and calculate standardized train scores
        for k, estimator in enumerate(self.estimator_list):
            estimator.fit(self.X_train_norm_)
            train_scores[:, k] = estimator.decision_scores_
        self.train_scores_ = train_scores

        # set decision scores and threshold
        self.decision_scores_ = self._get_decision_scores(X)
        self._process_decision_scores()

        return

    def decision_function(self, X):
        """ Predict outlier scores on test data X (note: model must already be fit)

        Parameters
        ----------
        X : numpy array, shape (n_samples, n_features)
            Test data

        Returns
        -------
        decision_scores : numpy array, shape (n_samples)
            Outlier scores for test samples
        """
        # check whether model has been fit
        check_is_fitted(self, ['training_pseudo_label_', 'train_scores_', 'X_train_norm_', 'n_features_'])

        # check input array
        X = check_array(X)
        if self.n_features_ != X.shape[1]:
            raise ValueError("Number of features of the model must "
                             "match the input. Model n_features is {0} and "
                             "input n_features is {1}."
                             "".format(self.n_features_, X.shape[1]))

        # get decision scores and return
        decision_scores = self._get_decision_scores(X)
        return decision_scores

    def _get_decision_scores(self, X):
        """ Helper function for getting outlier scores on test data X (note: model must already be fit)

        Parameters
        ----------
        X : numpy array, shape (n_samples, n_features)
            Test data

        Returns
        -------
        pred_scores_ens : numpy array, shape (n_samples,)
            Outlier scores for test samples
        """

        # raise warning if local region size is outside acceptable limits
        if (self.local_region_size < self.local_region_min) or (self.local_region_size > self.local_region_max):
            warnings.warn("Local region size of {} is outside recommended range [{}, {}]".format(
                self.local_region_size, self.local_region_min, self.local_region_max))

        # standardize test data and get local region for each test instance
        X_test_norm = X
        test_local_regions = self._get_local_region(X_test_norm)

        # calculate test scores
        test_scores = np.zeros([X_test_norm.shape[0], self.n_clf])
        for k, estimator in enumerate(self.estimator_list):
            test_scores[:, k] = estimator.decision_function(X_test_norm)

        # generate standardized scores
        train_scores_norm, test_scores_norm = standardizer(self.train_scores_, test_scores)

        # generate pseudo target for training --> for calculating weights
        self.training_pseudo_label_ = np.max(train_scores_norm, axis=1).reshape(-1, 1)

        # placeholder for ensemble predictions
        pred_scores_ens = np.zeros([X_test_norm.shape[0], ])

        # iterate through test instances (test_local_regions indices correspond to x_test)
        for i, test_local_region in enumerate(test_local_regions):

            # get pseudo target and training scores in local region of test instance
            local_pseudo_ground_truth = self.training_pseudo_label_[test_local_region,].ravel()
            local_train_scores = train_scores_norm[test_local_region, :]

            # calculate pearson correlation between local pseudo ground truth and local train scores
            pearson_corr_scores = np.zeros([self.n_clf, ])
            for d in range(self.n_clf):
                pearson_corr_scores[d,] = pearsonr(local_pseudo_ground_truth, local_train_scores[:, d])[0]

            # return best score
            pred_scores_ens[i,] = np.mean(test_scores_norm[i, self._get_competent_detectors(pearson_corr_scores)])

        return pred_scores_ens

    def _get_local_region(self, X_test_norm):
        """ Get local region for each test instance

        Parameters
        ----------
        X_test_norm : numpy array, shape (n_samples, n_features)
            Normalized test data

        Returns
        -------
        final_local_region_list : List of lists, shape [n_samples [local_region]]
            Indices of training samples in the local region of each test sample
        """

        # Initialize the local region list
        local_region_list = [[]] * X_test_norm.shape[0]

        # perform multiple iterations
        for _ in range(self.local_region_iterations):

            # randomly generate feature subspaces
            features = generate_bagging_indices(self.random_state,
                                                bootstrap_features=False,
                                                n_features=self.X_train_norm_.shape[1],
                                                min_features=int(self.X_train_norm_.shape[1] * self.local_min_features),
                                                max_features=int(self.X_train_norm_.shape[1] * self.local_max_features))

            # build KDTree out of training subspace
            tree = KDTree(self.X_train_norm_[:, features])

            # Find neighbors of each test instance
            _, ind_arr = tree.query(X_test_norm[:, features],
                                    k=self.local_region_size)

            # add neighbors to local region list
            for j in range(X_test_norm.shape[0]):
                local_region_list[j] = local_region_list[j] + ind_arr[j, :].tolist()

        # keep nearby points which occur at least local_region_threshold times
        final_local_region_list = [[]] * X_test_norm.shape[0]
        for j in range(X_test_norm.shape[0]):
            final_local_region_list[j] = [item for item, count in
                                          collections.Counter(local_region_list[j]).items() if
                                          count > self.local_region_threshold]

        return final_local_region_list

    def _get_competent_detectors(self, scores):
        """ Identifies competent base detectors based on correlation scores

        Parameters
        ----------
        scores : numpy array, shape (n_clf,)
            Correlation scores for each classifier (for a specific test instance)

        Returns
        -------
        candidates : List
            Indices for competent detectors (for given test instance)
        """

        # create histogram of correlation scores
        scores = scores.reshape(-1, 1)
        if self.n_bins > self.n_clf:
            warnings.warn("Number of histogram bins greater than number of classifiers, reducing n_bins to n_clf.")
            self.n_bins = self.n_clf
        hist, bin_edges = np.histogram(scores, bins=self.n_bins)

        # find n_selected largest bins
        max_bins = argmaxn(hist, n=self.n_selected)
        candidates = []

        # iterate through bins
        for max_bin in max_bins:
            # determine which detectors are inside this bin
            selected = np.where((scores >= bin_edges[max_bin])
                                & (scores <= bin_edges[max_bin + 1]))

            # add to list of candidates
            candidates = candidates + selected[0].tolist()

        return candidates

    def __len__(self):
        return len(self.estimator_list)

    def __getitem__(self, index):
        return self.estimator_list[index]

    def __iter__(self):
        return iter(self.estimator_list)
