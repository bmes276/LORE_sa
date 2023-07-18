from abc import abstractmethod

from joblib import Parallel, delayed
import multiprocessing as ml
import math
import pickle
from functools import partial

from scipy.spatial.distance import cdist
from sklearn.metrics import accuracy_score

from lore_sa.explanation import Explanation
from lore_sa.neighgen.neighborhood_generator import NeighborhoodGenerator
from lore_sa.surrogate import Surrogate
from lore_sa.util import neuclidean, record2str
from lore_sa.discretizer import Discretizer
from lore_sa.encoder_decoder import EncDec
from lore_sa.bbox import AbstractBBox
from lore_sa.dataset import TabularDataset
from lore_sa.rule import Rule
from lore_sa.rule import DecisioTreeRuleEmitter
import numpy as np


class Explainer():

    def __init__(self, dataset: TabularDataset, bb: AbstractBBox):
        pass

    @abstractmethod
    def fit(self, X, y, config):
        pass

    @abstractmethod
    def explain(self, b, x):
        pass


def default_kernel(d, kernel_width):
    return np.sqrt(np.exp(-(d ** 2) / kernel_width ** 2))

class LOREM(Explainer):
    """
    LOcal Rule-based Explanation Method Class

    LOREM is an explanator class initialized with a Dataset Object, a BlackBox Object, a config dictionary and a
    class_name list of string.

    :param TabularDataset dataset: Dataset Class that incapsulate the data and provides datamanager functions
    :param AbstractBBox bb:  Black Box
    :param NeighborhoodGenerator encdec: NeighborhoodGenerator object
    :param Surrogate surrogate: Surrogate object
    :param Rule rule: rule object
    :param list class_name: list of class names.
    :param K_transformed:
    :param bool multi_label:
    :param bool filter_crules:
    :param int kernel_width:
    :param kernel:
    :param int random_state:
    :param bool binary:
    :param Discretized discretize:
    :param bool extreme_fidelity:
    :param constraints:
    :param bool verbose:

    """
    def __init__(self, dataset: TabularDataset, bb: AbstractBBox, encdec: EncDec,neigh_gen: NeighborhoodGenerator,
                 surrogate: Surrogate, rule: Rule, class_name: str, K_transformed=None,
                 multi_label=False, filter_crules=True, kernel_width=None, kernel=None, random_state=None, binary=False,
                 discretize: Discretizer = None, extreme_fidelity: bool = False, constraints=None,
                 verbose: bool = False, **kwargs):

        super().__init__(dataset=dataset, bb=bb)
        self.dataset = dataset
        self.surrogate = surrogate
        self.neigh_gen = neigh_gen
        self.class_name = class_name
        self.bb_predict = bb.predict
        self.bb_predict_proba = bb.predict_proba
        self.rule = rule
        self.verbose = verbose

        self.encdec = encdec
        if self.encdec is not None:
            Y = self.bb_predict(self.dataset.df)
            self.K = self.encdec.encode(self.dataset.df, Y)
        else:
            self.K = self.dataset.df

        self.unadmittible_features = None
        self.df, self.feature_names, self.class_values, self.numeric_columns, self.rdf,self.real_feature_names, self.features_map = dataset.prepare_dataset(self.encdec)

        self.neigh_gen = neigh_gen
        self.multi_label = multi_label
        self.filter_crules = self.bb_predict if filter_crules else None
        self.binary = binary
        self.discretize = discretize
        self.extreme_fidelity = extreme_fidelity

        self.K_original = K_transformed
        self.features_map_inv = None
        if self.features_map:
            self.features_map_inv = dict()
            for idx, idx_dict in self.features_map.items():
                for k, v in idx_dict.items():
                    self.features_map_inv[v] = idx
        self.constraints = constraints

        kernel_width = np.sqrt(len(self.feature_names)) * .75 if kernel_width is None else kernel_width
        self.kernel_width = float(kernel_width)

        kernel = default_kernel if kernel is None else kernel
        self.kernel = partial(kernel, kernel_width=kernel_width)

        self.random_state = random_state
        np.random.seed(self.random_state)

    def __calculate_weights__(self, Z, metric):
        if np.max(Z) != 1 and np.min(Z) != 0:
            Zn = (Z - np.min(Z)) / (np.max(Z) - np.min(Z))
            distances = cdist(Zn, Zn[0].reshape(1, -1), metric=metric).ravel()
        else:
            distances = cdist(Z, Z[0].reshape(1, -1), metric=metric).ravel()
        weights = self.kernel(distances)
        return weights

    def get_feature_importance_supert(self, dt, x, tot_samples):
        dt.set_impurity(dt)
        dt.calculate_features_importance(tot_samples)
        all_features = dt.calculate_all_importances()
        single_features = dt.calculate_fi_path(x)
        return single_features, all_features

    def get_feature_importance_binary(self, dt, x):
        att_list = []
        # dt.apply: Return the index of the leaf that each sample is predicted as.
        leave_id_dt = dt.apply(x.reshape(1, -1))
        node_index_dt = dt.decision_path(x.reshape(1, -1)).indices
        feature_dt = dt.tree_.feature
        for node_id in node_index_dt:
            if leave_id_dt[0] == node_id:
                break
            else:
                att = self.feature_names[feature_dt[node_id]]
                att_list.append(att)
        # Feature importance
        feature_importance_all = dt.feature_importances_
        dict_feature_importance = dict(zip(self.feature_names, feature_importance_all))
        feature_importance_rule = {k: v for k, v in dict_feature_importance.items() if k in att_list}
        return feature_importance_rule, dict_feature_importance


    # qui l'istanza arriva originale
    def explain_instance_stable(self, x, samples=100, use_weights=True, metric=neuclidean, runs=3, exemplar_num=5, n_jobs=-1, prune_tree=False, single=False, kwargs=None):
        """
        This function explain Instance Stable

        :param x: instance to explain as numpy array
        :param samples: number of samples to generate during the neighbourhood generation
        :param use_weights: True or False
        :param metric: default is neuclidean, it is the metric employed to measure the distance between records
        :param runs: number of times the neighbourhood generation is done
        :param exemplar_num: number of examplars to retrieve
        :param n_jobs:
        :param prune_tree:
        :param single:
        :param kwargs: a dictionary in which add the parameters needed for cfs generation
        :return:
        """

        if self.multi_label:
            print('Not yet implemented')
            raise Exception

        if self.encdec is not None:
            y = self.bb_predict(x.reshape(1, -1))
            x = self.encdec.enc(x, y)

        Z_list = self.neigh_gen.multi_generate(x, samples, runs)

        Yb_list = list()
        # print('la Z creata ', len(Z_list), Z_list[0])
        if self.encdec is not None:
            for Z in Z_list:
                Z = self.encdec.dec(Z)
                # print('Z decodificata ', Z)
                Z = np.nan_to_num(Z)
                Yb = self.bb_predict(Z)
                # print('la yb ', Counter(Yb))
                Yb_list.append(Yb)
        else:
            if single:
                Yb = self.bb_predict(Z_list)
                Yb_list.append(Yb)
            else:
                for Z in Z_list:
                    Yb = self.bb_predict(Z)
                    Yb_list.append(Yb)

        if self.verbose:
            neigh_class_counts_list = list()
            for Yb in Yb_list:
                neigh_class, neigh_counts = np.unique(Yb, return_counts=True)
                neigh_class_counts = {self.class_values[k]: v for k, v in zip(neigh_class, neigh_counts)}
                neigh_class_counts_list.append(neigh_class_counts)

            for neigh_class_counts in neigh_class_counts_list:
                print('Synthetic neighborhood class counts %s' % neigh_class_counts)

        weights_list = list()
        if single:
            weights = None if not use_weights else self.__calculate_weights__(Z_list, metric)
            weights_list.append(weights)
        # print('la shape di z e come e fatta ', len(Z_list), Z[0].dtype)
        else:
            for Z in Z_list:
                # print('nel calcolo del peso', Z.dtype, Z.shape)
                weights = None if not use_weights else self.__calculate_weights__(Z, metric)
                weights_list.append(weights)

        if self.verbose:
            print('Learning local decision trees')

        # discretize the data employed for learning decision tree
        if self.discretize is not None:
            if single:
                discr = self.discretize
                discr.fit(Z, Yb)
                Z_list = discr.transform(Z_list)
            else:
                Z = np.concatenate(Z_list)
                Yb = np.concatenate(Yb_list)

                discr = self.discretize
                discr.fit(Z, Yb)
                temp = list()
                for Zl in Z_list:
                    temp.append(discr.transform(Zl))
                Z_list = temp

        # caso binario da Z e Y da bb
        if self.binary == 'binary_from_bb':
            surr = self.surrogate
            weights = None if not use_weights else self.__calculate_weights__(Z, metric)
            superT = surr.learn_local_decision_tree(Z, Yb, weights, self.class_values)
            fidelity = superT.score(Z, Yb, sample_weight=weights)

        # caso binario da Z e Yb
        # caso n ario
        # caso binario da albero n ario
        else:
            # qui prima creo tutti i dt, che servono sia per unirli con metodo classico o altri
            dt_list = [self.surrogate for i in range(runs)]
            dt_list = Parallel(n_jobs=n_jobs, verbose=self.verbose, prefer='threads')(
                delayed(t.learn_local_decision_tree)(Zl, Yb, weights, self.class_values, prune_tree=prune_tree)
                for Zl, Yb, weights, t in zip(Z_list, Yb_list, weights_list, dt_list))

            Z = np.concatenate(Z_list)
            Z = np.nan_to_num(Z)
            Yb = np.concatenate(Yb_list)

            # caso binario da Z e Yb dei vari dt
            if self.binary == 'binary_from_dts':
                weights = None if not use_weights else self.__calculate_weights__(Z, metric)
                surr = self.surrogate
                superT = surr.learn_local_decision_tree(Z, Yb, weights, self.class_values)
                fidelity = superT.score(Z, Yb, sample_weight=weights)

            # caso n ario
            # caso binario da albero n ario
            else:
                if self.verbose:
                    print('Pruning decision trees')
                surr = self.surrogate
                for t in dt_list:
                    surr.prune_duplicate_leaves(t)
                if self.verbose:
                    print('Merging decision trees')

                weights_list = list()
                for Zl in Z_list:
                    weights = None if not use_weights else self.__calculate_weights__(Zl, metric)
                    weights_list.append(weights)
                weights = np.concatenate(weights_list)
                n_features = list()
                for d in dt_list:
                    n_features.append(list(range(0, len(self.feature_names))))
                roots = np.array([surr.rec_buildTree(t, FI_used) for t, FI_used in zip(dt_list, n_features)])

                superT = surr.mergeDecisionTrees(roots, num_classes=np.unique(Yb).shape[0], verbose=False)

                if self.binary == 'binary_from_nari':
                    superT = surr.supert2b(superT, Z)
                    Yb = superT.predict(Z)
                    fidelity = superT.score(Z, Yb, sample_weight=weights)
                else:
                    Yz = superT.predict(Z)
                    fidelity = accuracy_score(Yb, Yz)

                if self.extreme_fidelity:
                    res = superT.predict(x)
                    if res != y:
                        raise Exception('The prediction of the surrogate model is different wrt the black box')

                if self.verbose:
                    print('Retrieving explanation')
        x = x.flatten()
        Yc = superT.predict(X=Z)
        rule = DecisioTreeRuleEmitter().get_rule(x, self.bb_predict(x.reshape(1, -1)), superT, encdec=self.encdec,
                                                 multi_label=self.multi_label)

        crules, deltas = DecisioTreeRuleEmitter().get_counterfactual_rules(x, Yc[0], superT, Z, Yc, self.feature_names,
                                                                           self.class_name, self.class_values, self.numeric_columns,
                                                                           self.features_map, self.features_map_inv, encdec=self.encdec,
                                                                           filter_crules=self.filter_crules, constraints=self.constraints)


        exp = Explanation()
        exp.bb_pred = Yb[0]
        exp.dt_pred = Yc[0]
        exp.rule = rule
        exp.crules = crules
        exp.deltas = deltas
        exp.dt = superT
        exp.fidelity = fidelity
        # Feature Importance
        if self.binary:
            feature_importance, feature_importance_all = self.get_feature_importance_binary(superT, x)
            exemplars_rec, cexemplars_rec = self.get_exemplars_cexemplars_binary(superT, x, exemplar_num)
        else:
            feature_importance, feature_importance_all = self.get_feature_importance_supert(superT, x, len(Yb))
            exemplars_rec, cexemplars_rec = self.get_exemplars_cexemplars_supert(superT, x, exemplar_num)
        # Exemplar and Counter-exemplar

        if exemplars_rec is not None:
            print('entro con exemplars ', exemplars_rec, self.feature_names)
            exemplars = self.get_exemplars_str(exemplars_rec)
        else:
            exemplars = 'None'
        if cexemplars_rec is not None:
            cexemplars = self.get_exemplars_str(cexemplars_rec)
        else:
            cexemplars = 'None'
        exp.feature_importance = feature_importance
        exp.feature_importance_all = feature_importance_all
        exp.exemplars = exemplars
        exp.cexemplars = cexemplars
        return exp

    def get_exemplars_str(self, exemplars_rec):
        exemplars = '\n'.join(
            [record2str(s, self.feature_names, self.numeric_columns, encdec=self.encdec) for s in exemplars_rec])
        return exemplars

    def get_exemplars_cexemplars_binary(self, dt, x, n):
        if self.encdec is not None:
            dataset = self.dataset.copy(deep=True)
            labels = dataset.pop(self.class_name)
            dataset = self.encdec.enc(dataset.values, labels.values)
            leave_id_K = dt.apply(dataset)
        else:
            print('la self k ', self.K)
            leave_id_K = dt.apply(self.K)

        leave_id_x = dt.apply(x.reshape(1, -1))
        exemplar_idx = np.where(leave_id_K == leave_id_x)
        if self.encdec is not None:
            exemplar_vals = dataset[exemplar_idx]
        else:
            exemplar_vals = self.K[exemplar_idx]

        cexemplar_idx = np.where(leave_id_K != leave_id_x)
        if self.encdec is not None:
            cexemplar_vals = dataset[cexemplar_idx]
        else:
            cexemplar_vals = self.K[cexemplar_idx]

        # find instance x in obtained list and remove it
        idx_to_remove = None
        if x in exemplar_vals:
            idx_to_remove = np.where((exemplar_vals == x).all(axis=1))[0]
        if idx_to_remove is not None:
            exemplar_vals = np.delete(exemplar_vals, idx_to_remove, axis=0)
        print('exemplars ', exemplar_vals, cexemplar_vals)
        if len(exemplar_vals) == 0 and len(cexemplar_vals) == 0:
            print('IN CASO NONE NONE vals', exemplar_vals, cexemplar_vals)
            return None, None
        elif len(exemplar_vals) == 0:
            print('CASO DI C EX E NONE')
            distance_x_cexemplar = cdist(x.reshape(1, -1), cexemplar_vals, metric='euclidean').ravel()
            n = len(cexemplar_vals)
            first_n_dist_id_c = distance_x_cexemplar.argsort()[:n]
            first_n_cexemplar = cexemplar_vals[first_n_dist_id_c]
            return None, first_n_cexemplar
        else:
            print('CASO DI EX E NONE')
            distance_x_exemplar = cdist(x.reshape(1, -1), exemplar_vals, metric='euclidean').ravel()
            n = len(exemplar_vals)
            first_n_dist_id = distance_x_exemplar.argsort()[:n]
            first_n_exemplar = exemplar_vals[first_n_dist_id]
            return first_n_exemplar, None

        distance_x_exemplar = cdist(x.reshape(1, -1), exemplar_vals, metric='euclidean').ravel()
        distance_x_cexemplar = cdist(x.reshape(1, -1), cexemplar_vals, metric='euclidean').ravel()

        if len(exemplar_vals) < n or len(cexemplar_vals) < n:
            if self.verbose:
                print('maximum number of exemplars and counter-exemplars founded is : %s, %s', len(exemplar_vals),
                      len(cexemplar_vals))
            n = min(len(cexemplar_vals), len(exemplar_vals))
        first_n_dist_id = distance_x_exemplar.argsort()[:n]
        first_n_exemplar = exemplar_vals[first_n_dist_id]

        first_n_dist_id_c = distance_x_cexemplar.argsort()[:n]
        first_n_cexemplar = cexemplar_vals[first_n_dist_id_c]

        return first_n_exemplar, first_n_cexemplar

    def get_exemplars_cexemplars_supert(self, dt, x, n):
        if self.encdec is not None:
            dataset = self.dataset.copy(deep=True)
            labels = dataset.pop(self.class_name)
            dataset = self.encdec.enc(dataset.values, labels.values)
            leave_id_K = dt.apply(dataset)
        else:
            leave_id_K = dt.apply(self.K)

        print('leave id applied ', leave_id_K)

        leave_id_x = dt.apply(x.reshape(1, -1))

        exemplar_idx = np.where(leave_id_K == leave_id_x)
        print('exemplar idx ', len(exemplar_idx))

        if self.encdec is not None:
            exemplar_vals = dataset[exemplar_idx]
        else:
            exemplar_vals = self.K[exemplar_idx]
        cexemplar_idx = np.where(leave_id_K != leave_id_x)
        print('cexemplar idx ', len(cexemplar_idx))
        if self.encdec is not None:
            cexemplar_vals = dataset[cexemplar_idx]
        else:
            cexemplar_vals = self.K[cexemplar_idx]
        print('exemplar and counter exemplars ', exemplar_vals, cexemplar_vals)
        # find instance x in obtained list and remove it
        idx_to_remove = None
        if x in exemplar_vals:
            print('cerco la x')
            idx_to_remove = np.where((exemplar_vals == x).all(axis=1))[0]
        if idx_to_remove is not None:
            print('la tolgo')
            exemplar_vals = np.delete(exemplar_vals, idx_to_remove, axis=0)

        distance_x_exemplar = cdist(x.reshape(1, -1), exemplar_vals, metric='euclidean').ravel()
        distance_x_cexemplar = cdist(x.reshape(1, -1), cexemplar_vals, metric='euclidean').ravel()

        if len(exemplar_vals) < n or len(cexemplar_vals) < n:
            if self.verbose:
                print('maximum number of exemplars and counter-exemplars founded is : %s, %s', len(exemplar_vals),
                      len(cexemplar_vals))
            n = min(len(cexemplar_vals), len(exemplar_vals))
        first_n_dist_id = distance_x_exemplar.argsort()[:n]
        first_n_exemplar = exemplar_vals[first_n_dist_id]

        first_n_dist_id_c = distance_x_cexemplar.argsort()[:n]
        first_n_cexemplar = cexemplar_vals[first_n_dist_id_c]

        return first_n_exemplar, first_n_cexemplar

    def explain_set_instances_stable(self, X, n_workers, title, runs=3, n_jobs=4, n_samples=1000, exemplar_num=5,
                                     use_weights=True, metric=neuclidean, kwargs=None):
        # for parallelization
        items_for_worker = math.ceil(len(X) / float(n_workers))
        start = 0
        print(items_for_worker)
        end = int(items_for_worker)
        processes = list()
        # create workers
        print("Dispatching jobs to workers...\n")
        for i in range(0, n_workers):
            print('start, end ', start, end)
            dataset = X[start:end]
            process = ml.Process(target=self.explain_workers_stable, args=(
            i, dataset, title, n_samples, use_weights, metric, runs, n_jobs, exemplar_num, kwargs))
            processes.append(process)
            process.start()

            if end > (len(X) - 1):
                workers = n_workers - 1
                break
            start = end
            end += int(items_for_worker)

        # join workers
        for i in range(0, workers):
            processes[i].join()
        print("All workers joint.\n")

    def explain_workers_stable(self, i, dataset, title, n_samples, use_wieghts, metric, runs=3, n_jobs=4,
                               exemplar_num=5, kwargs=None):
        count = 0
        results = list()
        title = 'explanations_lore' + title + '_' + str(i) + '.p'
        for d in dataset:
            print(i, count)
            count += 1
            d = np.array(d)
            exp = self.explain_instance_stable(d, samples=n_samples, use_weights=use_wieghts, metric=metric, runs=runs,
                                               exemplar_num=exemplar_num, n_jobs=n_jobs, kwargs=kwargs)
            results.append((d, exp))

        with open(title, "ab") as pickle_file:
            pickle.dump(results, pickle_file)

        return

    def set_unfeasibible_features(self, unadmittible_features):
        self.check_feasibility = True
        self.unadmittible_features = unadmittible_features