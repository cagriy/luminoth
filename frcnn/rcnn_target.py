import tensorflow as tf
import sonnet as snt
import numpy as np

from .utils.bbox_transform import bbox_transform, unmap
from .utils.bbox import bbox_overlaps


class RCNNTarget(snt.AbstractModule):
    """
    Generate RCNN target tensors for both probabilities and bounding boxes.

    TODO: We should unify this module with AnchorTarget.
    """
    def __init__(self, num_classes, name='rcnn_proposal'):
        super(RCNNTarget, self).__init__(name=name)
        self._num_classes = num_classes
        self._foreground_fraction = 0.25
        self._batch_size = 64
        self._foreground_threshold = 0.5
        self._background_threshold_high = 0.5
        self._background_threshold_low = 0.1

    def _build(self, proposals, gt_boxes):
        """
        Returns:
            TODO: Review implementetion with py-faster-rcnn ProposalTargetLayer
            TODO: It is possible to have two correct classes for a proposal?
        """
        (proposals_label, bbox_targets) = tf.py_func(
            self.proposal_target_layer,
            [proposals, gt_boxes],
            [tf.float32, tf.float32]
         )

        return proposals_label, bbox_targets

    def proposal_target_layer(self, proposals, gt_boxes):
        """
        First we need to calculate the true class of proposals based on gt_boxes.

        Args:
            proposals:
                Shape (num_proposals, 5) -> (batch, x1, y1, x2, y2)
                Are assumed to be inside the image.
            gt_boxes:
                Shape (num_gt, 4) -> (x1, y1, x2, y2)

        Returns:
            proposals_labels: (-1, 0, label) for each proposal.
                Shape (num_proposals,)
            bbox_targets: 4d bbox targets.
                Shape (num_proposals, 4)

        """
        np.random.seed(0)  # TODO: For reproducibility.

        # Remove batch id from proposals
        all_proposals = proposals[:,1:]

        # only keep anchors inside the image
        # TODO: We should do this when anchors are original generated in
        # network or does it fuck with our dimensions.
        inds_inside = np.where(
            (all_proposals[:, 0] >= 0) &
            (all_proposals[:, 1] >= 0) &
            (all_proposals[:, 2] < im_shape[1]) &
            (all_proposals[:, 3] < im_shape[0])
        )[0]

        # keep only inside all_proposals
        proposals = all_proposals[inds_inside, :]

        overlaps = bbox_overlaps(
            # We need to use float and ascontiguousarray because of Cython
            # implementation of bbox_overlap
            np.ascontiguousarray(proposals, dtype=np.float),
            np.ascontiguousarray(gt_boxes, dtype=np.float)
        )

        # overlaps returns (num_proposals, num_gt_boxes) with the IoU of
        # proposal P and ground truth box G in overlaps[P, G]

        # We are going to label each proposal based on the IoU with `gt_boxes`.
        # Start by filling the labels with -1, marking them as ignored.
        proposals_label = np.empty((proposals.shape[0], ), dtype=np.float32)
        proposals_label.fill(-1)

        # For each overlap there is three possible outcomes for labelling:
        #  if max(iou) < 0.1 then we ignore
        #  elif max(iou) <= 0.5 then we label background
        #  elif max(iou) > 0.5 then we label with the highest IoU in overlap
        max_overlaps = overlaps.max(axis=1)

        # Label background
        proposals_label[(max_overlaps > self._background_threshold_low) & (max_overlaps < self._background_threshold_high)] = 0

        # Filter proposal that have labels
        overlaps_with_label = max_overlaps >= self._foreground_threshold
        # Get label for proposal with labels
        overlaps_best_label = overlaps.argmax(axis=1)
        # Having the index of the gt bbox with the best label we need to get the label for
        # each gt box and sum it one because 0 is used for background.
        # we only assign to proposals with `overlaps_with_label`.
        proposals_label[overlaps_with_label] = (gt_boxes[:,4][overlaps_best_label] + 1)[overlaps_with_label]

        # proposals_label now has [0, num_classes + 1] for proposals we are
        # going to use and -1 for the ones we should ignore.

        # Now we subsample labels and mark them as -1 in order to ignore them.
        # Our batch of N should be: F% foreground (label > 0)
        num_fg = int(self._foreground_fraction * self._batch_size)
        fg_inds = np.where(proposals_label >= 1)[0]
        if len(fg_inds) > num_fg:
            disable_inds = np.random.choice(
                fg_inds, size=(len(fg_inds) - num_fg), replace=False
            )
            # We disable them with their negatives just to be able to debug
            # down the road.
            proposals_label[disable_inds] = - proposals_label[disable_inds]

        if len(fg_inds) < num_fg:
            tf.logging.warning(
                'We\'ve got only {} foreground samples instead of {}.'.format(
                len(fg_inds), num_fg
            ))

        # subsample negative labels
        num_bg = self._batch_size - np.sum(proposals_label >= 1)
        bg_inds = np.where(proposals_label == 0)[0]
        if len(bg_inds) > num_bg:
            disable_inds = np.random.choice(
                bg_inds, size=(len(bg_inds) - num_bg), replace=False
            )
            proposals_label[disable_inds] = -1

        """
        Next step is to calculate the proper targets for the proposals labeled
        based on the values of the ground-truth boxes.
        We have to use only the proposals labeled >= 1, each matching with
        the proper gt_boxes
        """

        # Get the ids of the proposals that matter for bbox_target comparisson.
        proposal_with_target_idx = np.nonzero(proposals_label > 0)[0]

        # Get top gt_box for every proposal, top_gt_idx shape (1000,) with values < gt_boxes.shape[0]
        top_gt_idx = overlaps.argmax(axis=1)

        # Get the corresponding ground truth box only for the proposals with target.
        gt_boxes_ids = top_gt_idx[proposal_with_target_idx]

        # Get the values of the ground truth boxes. This is shaped (num_proposals, 5) because we also have the label.
        proposals_gt_boxes = gt_boxes[gt_boxes_ids]

        # We create the same array but with the proposals
        proposals_with_target = proposals[proposal_with_target_idx]

        # We create our targets with bbox_transform
        bbox_targets = bbox_transform(proposals_with_target, proposals_gt_boxes)
        # TODO: We should normalize it in order for bbox_targets to have zero
        # mean and unit variance according to the paper.

        # We unmap `bbox_targets` to get back our final array shaped
        # `(num_proposals, 4)` filling the proposals with bbox target with 0.

        # We first unmap targets to proposal_labesl (containing the length of inside bboxes)
        bbox_targets = unmap(
            bbox_targets, proposals_label.shape[0], proposal_with_target_idx, fill=0)
        # Then we unmap to all_proposals with inds_inside.
        bbox_targets = unmap(
            bbox_targets, all_proposals.shape[0], inds_inside, fill=0)
        # Proposals labels doesn't need double unmap.
        proposals_label = unmap(
            proposals_label, all_proposals.shape[0], inds_inside, fill=-1)

        # TODO: Bbox targes now have shape (x, 4) but maybe it should have shape
        # (num_proposals, num_classes * 4).

        return proposals_label, bbox_targets