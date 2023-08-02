import copy
import datetime
import operator
from collections import defaultdict

import numpy as np

from lore_sa.dataset import TabularDataset
from lore_sa.encoder_decoder import EncDec, OneHotEnc
from lore_sa.logger import logger
from sklearn.tree._tree import TREE_LEAF
from sklearn.tree import DecisionTreeClassifier
import sklearn.model_selection

__all__ = ["Surrogate","DecisionTreeSurrogate"]

from lore_sa.rule import Expression, Rule
from lore_sa.surrogate.surrogate import Surrogate
from lore_sa.util import vector2dict, multilabel2str


class DecisionTreeSurrogate(Surrogate):

    def __init__(self, kind = None, preprocessing=None):
        super().__init__(kind, preprocessing)
        self.dt = None

    def train(self, Z, Yb, weights = None, class_values = None, multi_label: bool= False, one_vs_rest: bool = False, cv = 5, prune_tree: bool = False):
        """

        :param Z: The training input samples
        :param Yb: The target values (class labels) as integers or strings.
        :param weights: Sample weights.
        :param class_values:
        :param [bool] multi_label:
        :param [bool] one_vs_rest:
        :param [int] cv:
        :param [bool] prune_tree:
        :return:
        """
        self.dt = DecisionTreeClassifier()
        if prune_tree is True:
            param_list = {'min_samples_split': [ 0.01, 0.05, 0.1, 0.2, 3, 2],
                          'min_samples_leaf': [0.001, 0.01, 0.05, 0.1,  2, 4],
                          'splitter' : ['best', 'random'],
                          'max_depth': [None, 2, 10, 12, 16, 20, 30],
                          'criterion': ['entropy', 'gini'],
                          'max_features': [0.2, 1, 5, 'auto', 'sqrt', 'log2']
                          }

            if multi_label is False or (multi_label is True and one_vs_rest is True):
                if len(class_values) == 2 or (multi_label and one_vs_rest):
                    scoring = 'precision'
                else:
                    scoring = 'precision_macro'
            else:
                scoring = 'precision_samples'

            dt_search = sklearn.model_selection.HalvingGridSearchCV(self.dt, param_grid=param_list, scoring=scoring, cv=cv, n_jobs=-1)
            logger.info('Search the best estimator')
            logger.info('Start time: {0}'.format(datetime.datetime.now()))
            dt_search.fit(Z, Yb, sample_weight=weights)
            logger.info('End time: {0}'.format(datetime.datetime.now()))
            self.dt = dt_search.best_estimator_
            logger.info('Pruning')
            self.prune_duplicate_leaves(self.dt)
        else:
            self.dt.fit(Z, Yb)

        return self.dt


    def is_leaf(self, inner_tree, index):
        """Check whether node is leaf node"""
        return (inner_tree.children_left[index] == TREE_LEAF and
                inner_tree.children_right[index] == TREE_LEAF)


    def prune_index(self, inner_tree, decisions, index=0):
        """
        Start pruning from the bottom - if we start from the top, we might miss
        nodes that become leaves during pruning.
        Do not use this directly - use prune_duplicate_leaves instead.
        """
        if not self.is_leaf(inner_tree, inner_tree.children_left[index]):
            self.prune_index(inner_tree, decisions, inner_tree.children_left[index])
        if not self.is_leaf(inner_tree, inner_tree.children_right[index]):
            self.prune_index(inner_tree, decisions, inner_tree.children_right[index])

        # Prune children if both children are leaves now and make the same decision:
        if (self.is_leaf(inner_tree, inner_tree.children_left[index]) and
            self.is_leaf(inner_tree, inner_tree.children_right[index]) and
            (decisions[index] == decisions[inner_tree.children_left[index]]) and
            (decisions[index] == decisions[inner_tree.children_right[index]])):
            # turn node into a leaf by "unlinking" its children
            inner_tree.children_left[index] = TREE_LEAF
            inner_tree.children_right[index] = TREE_LEAF
            logger.info("Pruned {}".format(index))


    def prune_duplicate_leaves(self, dt):
        """Remove leaves if both"""
        decisions = dt.tree_.value.argmax(axis=2).flatten().tolist()  # Decision for each node
        self.prune_index(dt.tree_, decisions)



    def get_rule(self, x: np.array, dataset: TabularDataset, encdec: EncDec = None):
        """
        Extract the rules as the promises and consequences {p -> y}, starting from a Decision Tree

            >>> {( income > 90) -> grant),
                ( job = employer) -> grant)
            }

        :param [Numpy Array] x: instance encoded of the dataset to extract the rule
        :param [TabularDataset] dataset:
        :param [EncDec] encdec:
        :return [Rule]: Rule objects
        """
        x = x.reshape(1, -1)
        feature = self.dt.tree_.feature
        threshold = self.dt.tree_.threshold
        predicted_class = self.dt.predict(x)

        consequence = Expression(variable=dataset.class_name, operator=operator.eq, value=predicted_class)

        leave_id = self.dt.apply(x)
        node_index = self.dt.decision_path(x).indices

        feature_names = dataset.get_features_names()
        numeric_columns = dataset.get_numeric_columns()

        premises = list()
        for node_id in node_index:
            if leave_id[0] == node_id:
                break
            else:
                if encdec is not None:
                    if isinstance(encdec, OneHotEnc):
                        attribute = feature_names[feature[node_id]]
                        if attribute not in numeric_columns:
                            thr = False if x[0][feature[node_id]] <= threshold[node_id] else True
                            op = operator.eq
                        else:
                            thr = threshold[node_id]
                            op = operator.le if x[0][feature[node_id]] <= threshold[node_id] else operator.gt
                    else:
                        raise Exception('unknown encoder instance ')
                else:
                    op = operator.le if x[0][feature[node_id]] <= threshold[node_id] else operator.gt
                    attribute = feature_names[feature[node_id]]
                    thr = threshold[node_id]
                premises.append(Expression(attribute, op, thr))

        premises = self.compact_premises(premises)
        return Rule(premises, consequence)

    def compact_premises(self, premises_list):
        """
        Remove the same premises with different values of threashold

        :param premises_list: List of Expressions that defines the premises
        :return:
        """
        attribute_list = defaultdict(list)
        for premise in premises_list:
            attribute_list[premise.variable].append(premise)

        compact_plist = list()
        for att, alist in attribute_list.items():
            if len(alist) > 1:
                min_thr = None
                max_thr = None
                for av in alist:
                    if av.operator == operator.le:
                        max_thr = min(av.value, max_thr) if max_thr else av.value
                    elif av.operator == operator.gt:
                        min_thr = max(av.value, min_thr) if min_thr else av.value

                if max_thr:
                    compact_plist.append(Expression(att, operator.le, max_thr))

                if min_thr:
                    compact_plist.append(Expression(att, operator.gt, min_thr))
            else:
                compact_plist.append(alist[0])
        return compact_plist

    def get_counterfactual_rules(self, x, y, Z, Y, dataset: TabularDataset,
                                 features_map_inv, multi_label=False, encdec: EncDec = None, filter_crules=None,
                                 constraints=None, unadmittible_features=None):
        """

        :param [Numpy Array] x: instance encoded of the dataset
        :param [str] y: extracted class from the surrogate
        :param [Numpy Array] Z: Neighborhood instances
        :param Y: all possible classes provided by the surrogate
        :param [TabularDataset] dataset:
        :param features_map_inv:
        :param multi_label:
        :param [EncDec] encdec:
        :param filter_crules:
        :param constraints:
        :param unadmittible_features: List of unadmittible features
        :return:
        """

        class_values = dataset.get_class_values()
        class_name = dataset.class_name

        clen = np.inf
        crule_list = list()
        delta_list = list()
        Z1 = Z[np.where(Y != y)[0]]
        x_dict = vector2dict(x, dataset.get_features_names())
        for z in Z1:
            # estraggo la regola per ognuno
            crule = self.get_rule(x = z, dataset= dataset, encdec = encdec)

            delta = self.get_falsified_conditions(x_dict, crule)
            num_falsified_conditions = len(delta)

            if unadmittible_features is not None:
                is_feasible = self.check_feasibility_of_falsified_conditions(delta, unadmittible_features)
                if is_feasible is False:
                    continue

            if constraints is not None:

                to_remove = list()
                for p in crule.premises:
                    if p.att in constraints.keys():
                        if p.op == constraints[p.att]['op']:
                            if p.thr > constraints[p.att]['thr']:
                                break
                                # caso corretto
                            else:
                                to_remove.append()

            if filter_crules is not None:
                xc = self.apply_counterfactual(x, delta, dataset)
                bb_outcomec = filter_crules(xc.reshape(1, -1))[0]
                bb_outcomec = class_values[bb_outcomec] if isinstance(class_name, str) else multilabel2str(bb_outcomec,
                                                                                                           class_values)
                dt_outcomec = crule.cons

                if bb_outcomec == dt_outcomec:
                    if num_falsified_conditions < clen:
                        clen = num_falsified_conditions
                        crule_list = [crule]
                        delta_list = [delta]
                    elif num_falsified_conditions == clen:
                        if delta not in delta_list:
                            crule_list.append(crule)
                            delta_list.append(delta)
            else:
                if num_falsified_conditions < clen:
                    clen = num_falsified_conditions
                    crule_list = [crule]
                    delta_list = [delta]
                elif num_falsified_conditions == clen:
                    if delta not in delta_list:
                        crule_list.append(crule)
                        delta_list.append(delta)

        return crule_list, delta_list

    def get_falsified_conditions(self, x_dict: dict, crule: Rule):
        """
        Check the wrong conditions
        :param x_dict:
        :param crule:
        :return: list of falsified premises
        """
        delta = []
        for p in crule.premises:
            try:
                if p.operator == operator.le and x_dict[p.att] > p.value:
                    delta.append(p)
                elif p.operator == operator.gt and x_dict[p.att] <= p.value:
                    delta.append(p)
            except:
                print('pop', p.operator2string(), 'xd', x_dict, 'xd di p ', p.variable, 'hthrr', p.value)
                continue
        return delta

    def check_feasibility_of_falsified_conditions(self, delta, unadmittible_features: list):
        """
        Check if a falsifield confition is in an unadmittible feature list
        :param delta:
        :param unadmittible_features:
        :return: True or False
        """
        for p in delta:
            if p.variable in unadmittible_features:
                if unadmittible_features[p.variable] is None:
                    return False
                else:
                    if unadmittible_features[p.variable] == p.operator:
                        return False
        return True

    def apply_counterfactual(self, x, delta, dataset,  features_map=None, features_map_inv=None, numeric_columns=None):
        feature_names = dataset.get_features_names()
        x_dict = vector2dict(x, feature_names)
        x_copy_dict = copy.deepcopy(x_dict)
        for p in delta:
            if p.variable in numeric_columns:
                if p.value == int(p.value):
                    gap = 1.0
                else:
                    decimals = list(str(p.value).split('.')[1])
                    for idx, e in enumerate(decimals):
                        if e != '0':
                            break
                    gap = 1 / (10 ** (idx + 1))
                if p.operator == operator.gt:
                    x_copy_dict[p.variable] = p.value + gap
                else:
                    x_copy_dict[p.variable] = p.value
            else:
                fn = p.variable
                if p.operator == operator.gt:
                    if features_map is not None:
                        fi = list(feature_names).index(p.att)
                        fi = features_map_inv[fi]
                        for fv in features_map[fi]:
                            x_copy_dict['%s=%s' % (fn, fv)] = 0.0
                    x_copy_dict[p.att] = 1.0

                else:
                    if features_map is not None:
                        fi = list(feature_names).index(p.att)
                        fi = features_map_inv[fi]
                        for fv in features_map[fi]:
                            x_copy_dict['%s=%s' % (fn, fv)] = 1.0
                    x_copy_dict[p.att] = 0.0

        x_counterfactual = np.zeros(len(x_dict))
        for i, fn in enumerate(feature_names):
            x_counterfactual[i] = x_copy_dict[fn]

        return x_counterfactual