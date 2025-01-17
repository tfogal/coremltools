import numpy as np
import coremltools
from coremltools.models import datatypes, MLModel
from coremltools.models.neural_network import NeuralNetworkBuilder

from ..commons.basic_graph_ops import topsort, check_connections

from .graph_pass import *

try:
    import shapes
except:
    from . import shapes

DEBUG = False


def ssa_convert(ssa, top_func='main', inputs=None, outputs=None):
    """
    Convert NNSSA into CoreML spec.
    ssa - nnssa to be converted to CoreML spec.
    inputs - Input features of CoreML specs. Must be a list of tuples
             [(name, shape)], where name is the input's name, shape is the
             shape of the feature tensor. The shape must be static - all
             dimensions of shape should be a positive integer.
             When not provided, SSA converter will treat all input nodes
             in top level NNSSA as inputs.
    outputs - Output features of CoreML specs. Must be a list of [name].
              When not provided, SSA converter will treat all output nodes
              in top level NNSSA as outputs.
    """

    if outputs is not None:
        ssa.extract_subgraph(outputs, name=top_func)

    if DEBUG:
        import graphviz
        dot_string = ssa.get_dot_string(name_and_op_style=True)
        graphviz.Source(dot_string).view(filename='/tmp/ssa')

    # apply passes on the ssa, prior to conversion
    passes = [
        constant_weight_link_removal, fuse_bias_add,
        onehot_matmul_to_embedding,
        remove_single_isolated_node,
        transform_nhwc_to_nchw,
        remove_identity,  # This should be the last pass
    ]

    for p in passes:
        p(ssa)

    for f in list(ssa.functions.values()):
        check_connections(f.graph)

    if DEBUG:
        import graphviz
        dot_string = ssa.get_dot_string(name_and_op_style=True)
        graphviz.Source(dot_string).view(filename='/tmp/ssa_after_passes')

    converter = SSAConverter(ssa, top_func=top_func, inputs=inputs, outputs=outputs)
    converter.convert()
    mlmodel_spec = converter.get_spec()

    mlmodel_passes = [remove_disconnected_constants]
    for p in mlmodel_passes:
        p(mlmodel_spec)

    return mlmodel_spec


class SSAConverter(object):
    def __init__(self, net_ensemble, top_func='main', inputs=None, outputs=None):

        self.net_ensemble = net_ensemble
        self.top_func = top_func  # string indicating the top level function
        if self.top_func not in self.net_ensemble.functions:
            raise ValueError(
                'Top level function %s not in the NetworkEnsemble Provided' % self.top_func)

        # get top level inputs and outputs to instantiate spec
        self.net_ensemble.functions[top_func].find_inputs_and_outputs()
        top_input_names = list(map(str, self.net_ensemble.functions[top_func].inputs))
        top_output_names = list(map(str, self.net_ensemble.functions[top_func].outputs))
        top_ssa = self.net_ensemble.functions[top_func]

        # find_inputs_and_outputs() generates a list of required inputs, which
        # may not be supplied by inputs. We need to make sure that the user-supplied
        # inputs name and shape are consistent with the NNSSA.
        top_input_shapes = []
        for name in top_input_names:
            node = top_ssa.graph[name]
            shape = list(node.datatype.get_shape()) if hasattr(node.datatype, 'get_shape') else None
            if shape is None and inputs is None:
                raise ValueError(
                    'nnssa input "%s" has non-static shape %s, please provide in argument "inputs"'
                    % (name, str(shape)))
            if inputs is not None:
                if name not in inputs:
                    raise ValueError(
                        'Input "%s" is required by SSAConverter, but not passed in argument "inputs"' % name)
                if not shapes.is_static_shape(inputs[name]):
                    raise ValueError(
                        'Supplied input "%s" has non-static shape %s' % (name, inputs[name]))
                # Now that inputs[name] is deterministic, check whether it's a match for node's shape
                if not shapes.is_a_shape_of(inputs[name], shape):
                    raise ValueError(
                        'Input "%s" expects a shape compatible to %s, but is given %s' %
                        (name, str(shape), inputs[name]))
                # Now that we can use the shape to create top_input_shapes
                shape = inputs[name]
                if len(shape) == 0:  # Scalar should be interpreted as an 1-element array
                    shape = [1]
            else:
                # If input is None, use whatever there is
                if not shapes.is_static_shape(shape):
                    raise ValueError(
                        'nnssa input "%s" has non-static shape %s, please provide in argument "inputs"'
                        % (name, str(shape)))
            top_input_shapes.append(shape)

        top_input_types = [datatypes.Array(*dim) for dim in top_input_shapes]
        top_input_features = list(zip(top_input_names, top_input_types))

        # TODO - verify outputs
        if outputs is not None:
            for name in outputs:
                if name not in top_output_names:
                    raise ValueError('Output "%s" is not a nnssa output.' % name)

        top_output_features = list(zip(top_output_names, [None] * len(top_output_names)))

        self.top_builder = NeuralNetworkBuilder(
            input_features=top_input_features,
            output_features=top_output_features,
            disable_rank5_shape_mapping=True)

        self.spec = self.top_builder.spec

        self.CONVERT_FUNCTION_MAP = {
            'Placeholder': self._convert_input,
            'Const': self._convert_const,
            'Transpose': self._convert_transpose,
            'Shape': self._convert_shape,
            'StridedSlice': self._convert_slice,
            'Range': self._convert_range,
            'TensorArrayV3': self._convert_tensorarray_alloc,
            'Maximum': self._convert_maximum,
            'Minimum': self._convert_minimum,
            'TensorArrayScatterV3': self._convert_array_scatter,
            'make_tuple': self._convert_make_tuple,
            'while': self._convert_while,
            'function_entry': self._convert_function,
            'get_tuple': self._convert_get_tuple,
            'Less': self._convert_less,
            'NotEqual': self._convert_not_equal,
            'LogicalAnd': self._convert_logical_and,
            'Log': self._convert_log,
            'return': self._convert_return,
            'Add': self._convert_add,
            'Sub': self._convert_sub,
            'SquaredDifference': self._convert_squared_difference,
            'TensorArrayReadV3': self._convert_tensorarray_read,
            'TensorArrayWriteV3': self._convert_tensorarray_write,
            'ConcatV2': self._convert_concat_nd,
            'MatMul': self._convert_batched_mat_mul,
            'BatchMatMul': self._convert_batched_mat_mul,
            'Embedding': self._convert_embedding,
            'BiasAdd': self._convert_bias_add,
            'Split': self._convert_split,
            'Sigmoid': self._convert_sigmoid,
            'Relu': self._convert_relu,
            'LeakyRelu': self._convert_leaky_relu,
            'Tanh': self._convert_tanh,
            'Mul': self._convert_mul,
            'Identity': self._convert_identity,
            'Cast': self._convert_cast,
            'TensorArraySizeV3': self._convert_tensorarray_size,
            'TensorArrayGatherV3': self._convert_tensorarray_gather,
            'Pack': self._convert_pack,
            'Unpack': self._convert_unpack,
            'Gather': self._convert_gather,
            'Sqrt': self._convert_sqrt,
            'Rsqrt': self._convert_rsqrt,
            'Pow': self._convert_pow,
            'Conv2D': self._convert_conv2d,
            'Reshape': self._convert_reshape,
            'Softmax': self._convert_softmax,
            'Sum': self._convert_sum,
            'Mean': self._convert_mean,
            'ArgMax': self._convert_argmax,
            'ReverseV2': self._convert_reverse,
            'ReverseSequence': self._convert_reverse_sequence,
            'ExpandDims': self._convert_expand_dims,
        }

        # converter state variables
        # func_stack stores a list of NNSSA function names
        self.func_stack = [self.top_func]
        # Theoretically, there should be a one-to-one mapping between
        # SSA function and nn_spec, which is associated with a NeuralNetworkBuilder
        self.func_builder_map = {self.top_func: self.top_builder}
        # All the shapes of the tensor of CoreML str:shape
        self.tensor_shapes = {
            name: top_input_shapes[idx]
            for idx, name in enumerate(top_input_names)
        }
        # Map for tensors generated by special ops (make_tuple, get_tuple, function, return, etc)
        # and value is the list of node names that represent tensors
        self.op_tensor_map = {}

    def get_spec(self):
        return self.spec

    def print_function_nodes(self, func_name):
        if func_name not in self.net_ensemble.functions:
            raise ValueError('%s is not a function name in NetworkEnsemble' % (func_name))
        graph = self.net_ensemble.functions[func_name].graph
        for name, node in graph.items():
            if node.op == 'get_global':
                print('%s (%s) var = %s' % (name, node.op, node.attr['variable']))
            if node.op == 'set_global':
                print('%s (%s) var = %s' % (name, node.op, node.attr['variable']))

    def get_nnssa_inputs_outputs(self):
        inputs, outputs, placeholder_defaults = self.net_ensemble._get_inputs_outputs()
        print('Inputs: ')
        for i in inputs:
            print(i)
        print('Outputs: ')
        for o in outputs:
            print(o)
        print('Placeholders with default: ')
        for p in placeholder_defaults:
            print(p)

    def convert(self):
        """ Convert the NNSSA function on top of func_stack into NeuralNetworkSpec.
        """
        func_name = self.func_stack[-1]
        func = self.net_ensemble.functions[func_name]
        print('[SSAConverter] Converting function %s ...' % (func_name))
        # Do a topological sort
        # ?? Why leaving out nodes with all outputs with some value??
        # I'm assuming restricted_graph is enough to generate all layers
        restricted_graph = {}
        function = self.net_ensemble.functions[func_name]
        for k, v in function.graph.items():
            if len(v.outputs) > 0 and all([function.graph[i].value is not None for i in v.outputs]):
                print([function.graph[i].value is not None for i in v.outputs])
                continue
            restricted_graph[k] = v
        instruction_order = topsort(restricted_graph)

        for idx, node_name in enumerate(instruction_order):
            node = func.graph[node_name]
            op_type = node.op
            if op_type not in self.CONVERT_FUNCTION_MAP:
                raise NotImplementedError(
                    '[SSAConverter] Conversion for op %s not implemented, terminating...' %
                    (op_type))
            print(
                '[SSAConverter] [{}/{}] Converting op {}: {}'.format(
                    idx + 1, len(instruction_order), node_name, op_type))

            convert_func = self.CONVERT_FUNCTION_MAP[op_type]
            convert_func(node)

    def _get_builder(self, func=None):
        if func is None:
            func = self.func_stack[-1]
        return self.func_builder_map[func]

    def _get_input_tensors(self, node):
        """ Get the input tensors for a node.
        There are two cases:
        (1) (Tuple case) input is a tuple. In this case, expand that tuple input into a list of input tensors
        (2) (Regular case) input is a node name. In this case just copy it.
        (3) (Indexed tuple case) input is one element in a tuple. In this case it should be stored in op_tensor_map
        """
        input_tensors = []
        for name in node.inputs:
            if name in self.op_tensor_map:
                input_tensors.extend(self.op_tensor_map[name])
            else:
                input_tensors.append(name)
        return input_tensors

    def _get_current_graph(self):
        return self.net_ensemble.functions[self.func_stack[-1]].graph

    def _skip(self, node):
        # Simply pass
        pass

    def _convert_input(self, node):
        """ Convert an input node. For now, we may just need to skip it.
        """
        pass

    def _convert_const(self, node):
        """ Convert a constant node.
        """
        val = np.array(node.value.val)
        if len(val.shape) == 0:
            val = np.array([node.value.val])
        builder = self._get_builder()
        layer = builder.add_load_constant_nd(
            name=node.name, output_name=node.name, constant_value=val, shape=val.shape)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_transpose(self, node):
        """ Convert a transpose op.
        """
        # permute dimensions are assumed to be a const
        input_names = self._get_input_tensors(node)
        if len(input_names) > 1:
            dim = self._get_current_graph()[node.inputs[1]].value.val
        else:
            dim = node.attr.get('dim')
        if dim is None:
            raise ValueError('[SSAConverter] Cannot handle dynamic Transpose')
        dim = list(dim)
        input_names = self._get_input_tensors(node)
        builder = self._get_builder()

        layer = builder.add_transpose(
            name=node.name, axes=dim, input_name=input_names[0], output_name=node.name)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_shape(self, node):
        input_names = self._get_input_tensors(node)
        assert (len(input_names) == 1)
        builder = self._get_builder()
        layer = builder.add_get_shape(
            name=node.name, input_name=input_names[0], output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_slice(self, node):
        # Note: For simple RNN, node.attr always has a 'slice'; this means slicing is always static
        if 'slice' not in node.attr:
            raise NotImplementedError('Dynamic slicing not implemented')

        # each slice is [begin, end, step]
        slices = node.attr['slice']
        begin_indices, end_indices, strides = [], [], []
        for s in slices:
            begin_indices.append(s[0])
            end_indices.append(s[1])
            strides.append(s[2])

        input_names = self._get_input_tensors(node)

        has_squeeze = 'squeeze' in node.attr
        axes = node.attr.get('squeeze')
        output_shapes = node.attr.get('_output_shapes')
        output_shape = output_shapes[0] if output_shapes is not None and len(output_shapes) > 0 else None
        if has_squeeze:
            if output_shape is None:
                raise ValueError('[SSAConverter] Unable to determine output shapes for Slice')
            if len(output_shape) == 0 and len(axes) == 1:
                has_squeeze = False

        slice_output_name = node.name + '_slice_' if has_squeeze else node.name

        builder = self._get_builder()
        layer = builder.add_slice_static(
            name=slice_output_name,
            input_name=input_names[0],
            output_name=slice_output_name,
            begin_ids=begin_indices,
            end_ids=end_indices,
            strides=strides,
            begin_masks=[False] * len(slices),
            end_masks=[False] * len(slices))

        shapes.propagate_single_layer(layer, self.tensor_shapes)

        if has_squeeze:
            layer = builder.add_squeeze(
                name=node.name,
                input_name=slice_output_name,
                output_name=node.name,
                axes=axes)
            shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_range(self, node):
        builder = self._get_builder()
        input_names = self._get_input_tensors(node)
        if len(input_names) != 3:
            raise ValueError(
                'CoreML NeuralNetwork range layer must have 3 inputs: start, limit and step')
        input_names = [input_names[1], input_names[0], input_names[2]]
        layer = builder.add_range_static(name=node.name, output_name=node.name, input_names=input_names)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_tensorarray_alloc(self, node):
        # TensorArray is a list of tensors, it will be treated as a rank+1 tensor when converted
        # The shape information is stored at two different places - node input specifies the length of the list
        # while the node's datatype stores the shape of each of the tensor allocated.
        input_names = self._get_input_tensors(node)
        assert (len(input_names) == 1)

        # Get element shape
        from ..commons.builtins import get_type_info
        tp = get_type_info(node.datatype).tparam
        assert type(tp) is list
        element_shapes = [x.tparam[1:] for x in tp]
        # Check for shape consistency
        for es in element_shapes:
            if es != element_shapes[0]:
                raise ValueError(
                    '[SSAConverter] TensorArray allocation cannot handle arrays with tensors of various shapes'
                )
        es = element_shapes[0]

        array_size = None
        try:
            array_size = int(node.attr['size'])
        except:
            pass

        # Simpler case: No dynamic shape
        if array_size is not None:
            array_shape = [array_size] + es
            layer = self._get_builder().add_load_constant_nd(
                name=node.name,
                output_name=node.name,
                constant_value=np.zeros(array_shape, dtype='float'),
                shape=array_shape)
            shapes.propagate_single_layer(layer, self.tensor_shapes)
            return

        # Load element shape into network
        node_es_name = node.name + '__element_shape'
        builder = self._get_builder()
        layer = builder.add_load_constant_nd(
            name=node_es_name,
            output_name=node_es_name,
            constant_value=np.array(es, dtype='float'),
            shape=[len(es)])
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        # Concatenate list length (the input, should be a constant vector of size 1) with element shape
        node_arr_shape_name = node.name + '__arr_shape'
        layer = builder.add_concat_nd(
            name=node_arr_shape_name,
            input_names=input_names + [node_es_name],
            output_name=node_arr_shape_name,
            axis=0)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        # Now allocate required shape
        layer = builder.add_fill_dynamic(
            name=node.name, input_name=node_arr_shape_name, output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)
        # Overwrite the output shape with fixed element shape
        self.tensor_shapes[node.name][1:] = es
        layer.outputTensor[0].dimValue[1:] = es

    def _convert_maximum(self, node):
        assert (len(node.inputs) == 2)
        builder = self._get_builder()
        layer = builder.add_elementwise(
            name=node.name,
            input_names=self._get_input_tensors(node),
            output_name=node.name,
            mode='MAX')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_minimum(self, node):
        assert (len(node.inputs) == 2)
        builder = self._get_builder()
        layer = builder.add_elementwise(
            name=node.name,
            input_names=self._get_input_tensors(node),
            output_name=node.name,
            mode='MIN')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_array_scatter(self, node):
        # NNSSA input order: indices, value, array
        # CoreML input order: container (array), indices, slices (value)
        input_names = self._get_input_tensors(node)
        if len(input_names) != 3:
            raise ValueError('Scatter only accepts 3 inputs')
        indices, value, array = input_names
        layer = self._get_builder().add_scatter(
            name=node.name, input_names=[array, indices, value], output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_make_tuple(self, node):
        # make tuple aggregates a list of SSA nodes (which also stands for their outputs)
        # For now, I think recording the make_tuple node itself for reference would suffice.
        if node.name in self.op_tensor_map:
            raise ValueError('make_tuple node %s should not be visited twice.' % (node.name))
        self.op_tensor_map[node.name] = self._get_input_tensors(node)

    def _convert_while(self, node):
        # In CoreML, loops and branches should be designed such that inputs / outputs
        # should be empty, because it is not necessary and not clearly defined.
        # Should only take a tuples
        assert (len(node.inputs) == 1)
        current_graph = self.net_ensemble.functions[self.func_stack[-1]].graph
        assert (current_graph[node.inputs[0]].op == 'make_tuple')
        input_names = self._get_input_tensors(node)
        # print('[While Loop] input names:')
        # for i, name in enumerate(input_names):
        #     print('(%d) %s' %(i,name))

        self.op_tensor_map[node.name] = input_names
        builder_top = self._get_builder()
        while_layer = builder_top.add_loop(name=node.name)

        loop_param = while_layer.loop
        loop_param.maxLoopIterations = 0

        # Both body function and condition function share the same inputs (args) of the loop
        # convert the condition function
        if 'cond_function' in node.attr:
            if not loop_param.HasField('conditionNetwork'):
                loop_param.condition.MergeFromString(b'')
            cond_func_name = node.attr['cond_function']
            # TODO - need to find cond_var name
            self.func_stack.append(cond_func_name)
            self.func_builder_map[cond_func_name] = NeuralNetworkBuilder(
                nn_spec=loop_param.conditionNetwork, disable_rank5_shape_mapping=True)

            self.op_tensor_map[cond_func_name] = input_names
            self.convert()
            cond_func = self.net_ensemble.functions[cond_func_name]
            ret_node_name = cond_func.outputs[0]
            loop_param.conditionVar = cond_func.graph[ret_node_name].inputs[0]
            self.func_stack.pop()
        else:
            raise ValueError('Unable to determine condition function in the loop')

        # convert the body function
        if 'body_function' not in node.attr:
            raise ValueError('A "while" SSA node should not be empty.')
        if not loop_param.HasField('bodyNetwork'):
            loop_param.bodyNetwork.MergeFromString(b'')

        body_func_name = node.attr['body_function']
        self.func_stack.append(body_func_name)
        self.func_builder_map[body_func_name] = NeuralNetworkBuilder(
            nn_spec=loop_param.bodyNetwork, disable_rank5_shape_mapping=True)

        self.op_tensor_map[body_func_name] = input_names
        self.convert()

        # The body function should re-write variables when it returns.
        body_func = self.net_ensemble.functions[body_func_name]
        loop_var_tuple_name = None
        for k, v in body_func.graph.items():
            # k is name, v is node
            if v.op == 'make_tuple' and body_func.graph[v.outputs[0]].op == 'return':
                loop_var_tuple_name = k
                break

        loop_var_names = self.op_tensor_map[loop_var_tuple_name]
        assert (
                len(loop_var_names) == len(input_names)
        )  # Loop body should have the same input and output
        # print('[While Loop] output names:')
        # for i, name in enumerate(loop_var_names):
        #     print('(%d) %s' %(i,name))
        builder_body = self._get_builder()
        for src, dst in zip(loop_var_names, input_names):
            # loop variables may be passed as an input to while op but unused.
            if src == dst:
                continue
            layer = builder_body.add_copy(
                name='copy_' + src + '_' + dst, input_name=src, output_name=dst)
            shapes.propagate_single_layer(layer, self.tensor_shapes)
            # print('[While Loop] add copy from "%s" to "%s"' %(src, dst))

        # Pop back into while's loop
        self.func_stack.pop()

    def _convert_function(self, node):
        # Function node is the entry point of a function
        pass

    def _convert_get_tuple(self, node):
        input_names = self._get_input_tensors(node)
        self.op_tensor_map[node.name] = [input_names[node.attr['index']]]

    def _convert_return(self, node):
        # When converting a body function of a loop, return node should overwrite body functions' input tensors
        pass

    def _convert_less(self, node):
        assert (len(node.inputs) == 2)
        builder = self._get_builder()
        layer = builder.add_less_than(
            name=node.name, input_names=self._get_input_tensors(node), output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_not_equal(self, node):
        assert (len(node.inputs) == 2)
        builder = self._get_builder()
        layer = builder.add_not_equal(
            name=node.name, input_names=self._get_input_tensors(node), output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    # def add_not_equal(self, name, input_names, output_name, alpha=0):

    def _convert_logical_and(self, node):
        assert (len(node.inputs) == 2)
        layer = self._get_builder().add_logical(
            name=node.name,
            input_names=self._get_input_tensors(node),
            output_name=node.name,
            mode='AND')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_log(self, node):
        builder = self._get_builder()
        layer = builder.add_unary(
            name=node.name,
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name,
            mode='log')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_rsqrt(self, node):
        layer = self._get_builder().add_unary(
            name=node.name,
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name,
            mode='rsqrt')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_add(self, node):
        assert (len(node.inputs) == 2)
        layer = self._get_builder().add_add_broadcastable(
            name=node.name, input_names=self._get_input_tensors(node), output_name=node.name)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_sub(self, node):
        assert (len(node.inputs) == 2)
        layer = self._get_builder().add_subtract_broadcastable(
            name=node.name, input_names=self._get_input_tensors(node), output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_mul(self, node):
        assert (len(node.inputs) == 2)
        layer = self._get_builder().add_multiply_broadcastable(
            name=node.name, input_names=self._get_input_tensors(node), output_name=node.name)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_squared_difference(self, node):
        assert (len(node.inputs) == 2)
        sub_node_name = node.name + '_sub_'

        layer = self._get_builder().add_subtract_broadcastable(
            name=sub_node_name, input_names=self._get_input_tensors(node), output_name=sub_node_name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        layer = self._get_builder().add_unary(
            name=node.name, input_name=sub_node_name, output_name=node.name, mode='power', alpha=2.0)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_softmax(self, node):
        input_names = self._get_input_tensors(node)
        axis = -1 if 'axis' not in node.attr else node.attr['axis']
        layer = self._get_builder().add_softmax_nd(
            name=node.name, input_name=input_names[0], output_name=node.name, axis=axis)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_sum(self, node):
        input_names = self._get_input_tensors(node)
        input_shape = self._get_current_graph()[node.inputs[0]].attr['_output_shapes'][0]
        reduction_indices = node.attr['reduction_indices']

        keepdims = node.attr.get('keep_dims')
        if keepdims is None:
            keepdims = False

        layer = self._get_builder().add_reduce_sum(
            name=node.name,
            input_name=input_names[0],
            output_name=node.name,
            axes=reduction_indices,
            keepdims=keepdims,
            reduce_all=False)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_mean(self, node):
        input_names = self._get_input_tensors(node)
        input_shape = self._get_current_graph()[node.inputs[0]].attr['_output_shapes'][0]
        reduction_indices = node.attr['reduction_indices']
        keepdims = node.attr['keep_dims']

        layer = self._get_builder().add_reduce_mean(
            name=node.name,
            input_name=input_names[0],
            output_name=node.name,
            axes=reduction_indices,
            keepdims=keepdims,
            reduce_all=False)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_tensorarray_read(self, node):
        # TensorArrayReadV3 slices an element from TensorArray, which in NNSSA is a list.
        # This is equivalent to array gather
        input_names = self._get_input_tensors(node)
        slice_output_name = node.name + '_slice_'
        layer = self._get_builder().add_gather(
            name=node.name + '_gather_',
            input_names=input_names[::-1],
            output_name=slice_output_name,
            axis=0)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        # tensorarray_read should generate only 1 slice, so adding a squeeze should be enough
        layer = self._get_builder().add_squeeze(
            name=node.name + '_squeeze_',
            input_name=slice_output_name,
            output_name=node.name,
            axes=[0])
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_tensorarray_write(self, node):
        """def TensorArrayWrite(index, value, array):
        array[index] = value
        return array
        """
        # node.inputs = ['index', 'value', 'array']
        input_names = self._get_input_tensors(node)
        assert (len(input_names) == 3)
        index_name, value_name, array_name = input_names
        values_name = value_name + '_expanded'
        layer = self._get_builder().add_expand_dims(
            name=values_name, input_name=value_name, output_name=values_name, axes=[0])
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        # 3 inputs: [Scatter target, indices, scatter source]
        layer = self._get_builder().add_scatter(
            name=node.name,
            input_names=[array_name, index_name, values_name],
            output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_concat_nd(self, node):
        assert (len(node.inputs) > 1)

        input_names = self._get_input_tensors(node)
        g = self._get_current_graph()
        axis = node.attr.get('axis')
        if axis is None:
            axis = g[input_names[-1]].value
        if axis is None:
            raise NotImplementedError('[SSAConverter] Dynamic concatenation is not supported')
        axis = axis.val
        layer = self._get_builder().add_concat_nd(
            name=node.name, input_names=input_names[:-1], output_name=node.name, axis=axis)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_batched_mat_mul(self, node):
        input_names = self._get_input_tensors(node)
        g = self._get_current_graph()

        weight, bias = None, None
        if len(input_names) == 1:
            weight = node.attr.get('W', node.attr.get('W_const'))
            bias = node.attr.get('bias')
        elif len(input_names) == 2 and g[node.inputs[1]].op == 'Const':
            input_names = [input_names[0]]
            weight = g[node.inputs[1]].value.val
            bias = node.attr.get('bias')

        transpose_a = node.attr.get('adj_x', False) or node.attr.get('transpose_a', False)
        transpose_b = node.attr.get('adj_y', False) or node.attr.get('transpose_b', False)
        if len(input_names) == 1 and transpose_b and weight is not None:
            weight = weight.transpose((1, 0))

        n_rows = 0 if weight is None else weight.shape[0]
        n_cols = 0 if weight is None else weight.shape[1]
        builder = self._get_builder()
        layer = builder.add_batched_mat_mul(
            name=node.name,
            input_names=input_names,
            output_name=node.name,
            W=weight,  # (batched_mat_mul requires Cin, Cout)
            weight_matrix_rows=n_rows,
            weight_matrix_columns=n_cols,
            bias=bias,
            transpose_a=transpose_a,
            transpose_b=transpose_b)
        shapes.propagate_single_layer(layer, self.tensor_shapes)
        # if bias is not None and 2 in bias.shape:
        #     import pdb
        #     pdb.set_trace()
        #     print('2 in bias.shape')

    def _convert_bias_add(self, node):
        assert (len(node.inputs) == 2)
        input_names = self._get_input_tensors(node)

        layer = self._get_builder().add_add_broadcastable(
            name=node.name, input_names=input_names, output_name=node.name)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_split(self, node):
        # Only handles static, even splits
        axis = node.attr['split_dim']
        split = node.attr['split']
        if not all([x == split[0] for x in split]):
            raise NotImplementedError('[SSAConverter] Uneven split is not supported')
        num_splits = len(split)
        input_names = self._get_input_tensors(node)

        # Split output is a tuple. We need to split them into a list of tensors
        output_names = [(node.name + '_' + str(i) + '_') for i in range(num_splits)]
        if node.name in self.op_tensor_map:
            raise ValueError(
                '[SSAConverter] split node %s should not be visited twice.' % node.name)
        self.op_tensor_map[node.name] = output_names
        layer = self._get_builder().add_split_nd(
            name=node.name,
            input_name=input_names[-1],
            output_names=output_names,
            axis=axis,
            num_splits=num_splits)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_sigmoid(self, node):
        builder = self._get_builder()
        layer = builder.add_activation(
            name=node.name,
            non_linearity='SIGMOID',
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_relu(self, node):
        builder = self._get_builder()
        layer = builder.add_activation(
            name=node.name,
            non_linearity='RELU',
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_leaky_relu(self, node):
        builder = self._get_builder()
        layer = builder.add_activation(
            name=node.name,
            non_linearity='LEAKYRELU',
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name,
            params=([node.attr['alpha']]))
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_tanh(self, node):
        builder = self._get_builder()
        layer = builder.add_activation(
            name=node.name,
            non_linearity='TANH',
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_identity(self, node):
        layer = self._get_builder().add_activation(
            name=node.name,
            non_linearity='LINEAR',
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name,
            params=(1.0, 0.0))
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_tensorarray_size(self, node):
        input_names = self._get_input_tensors(node)
        assert (len(input_names) == 1)
        builder = self._get_builder()
        layer = builder.add_get_shape(
            name=node.name + '_full_shape',
            input_name=input_names[0],
            output_name=node.name + '_full_shape')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        layer = builder.add_slice_static(
            name=node.name,
            input_name=node.name + '_full_shape',
            output_name=node.name,
            begin_ids=[0],
            end_ids=[1],
            begin_masks=[False],
            end_masks=[False],
            strides=[1])
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_tensorarray_gather(self, node):
        input_names = self._get_input_tensors(node)
        assert (len(input_names) == 2)
        layer = self._get_builder().add_gather(
            name=node.name, input_names=input_names[::-1], output_name=node.name, axis=0)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_pack(self, node):
        input_names = self._get_input_tensors(node)
        layer = self._get_builder().add_stack(
            name=node.name, input_names=input_names, output_name=node.name, axis=0)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_unpack(self, node):
        input_names = self._get_input_tensors(node)
        output_names = node.outputs
        self.op_tensor_map[node.name] = output_names
        num_splits = node.attr['num']
        axis = int(node.attr['axis'])
        interm_output_names = [name + '_unsqueezed_' for name in output_names]
        layer = self._get_builder().add_split_nd(
            name=node.name, input_name=input_names[0], output_names=interm_output_names, axis=axis,
            num_splits=num_splits)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        for in_name, out_name in zip(interm_output_names, output_names):
            layer = self._get_builder().add_squeeze(
                name=out_name, input_name=in_name, output_name=out_name, axes=[0])
            shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_gather(self, node):
        input_names = self._get_input_tensors(node)
        # NNSSA: [u'encoder/Variable/read', u'Placeholder', u'encoder/embedding_lookup/axis']
        # CoreML         Given two inputs, 'data' and 'indices', gather the slices of 'data'
        axis = node.attr['axis']
        layer = self._get_builder().add_gather(
            name=node.name, input_names=input_names[0:2], output_name=node.name, axis=0)

        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_sqrt(self, node):
        input_names = self._get_input_tensors(node)
        layer = self._get_builder().add_unary(
            name=node.name, input_name=input_names[0], output_name=node.name, mode='sqrt')
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_pow(self, node):
        input_names = self._get_input_tensors(node)
        alpha_node = self._get_current_graph()[input_names[1]]
        if 'value' not in alpha_node.attr:
            import pdb
            pdb.set_trace()
        alpha = self._get_current_graph()[input_names[1]].attr['value'].val[0]
        layer = self._get_builder().add_unary(
            name=node.name,
            input_name=input_names[0],
            output_name=node.name,
            mode='power',
            alpha=alpha)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_conv2d(self, node):
        input_names = self._get_input_tensors(node)
        g = self._get_current_graph()

        weight = None
        bias = None
        if len(input_names) == 1:
            weight = node.attr.get('W', node.attr.get('W_const'))
            bias = node.attr.get('bias')
        elif len(input_names) == 2:
            input_names = [input_names[0]]
            if g[node.inputs[1]].op == 'Const':
                weight = g[node.inputs[1]].value.val
            bias = node.attr.get('bias')

        if weight is None:
            raise NotImplementedError(
                '[SSAConverter] Dynamic weights in convolution not implemented')

        assert (len(weight.shape) == 4, 'Conv2d: weight parameter not rank 4')

        data_format = node.attr.get('data_format', 'NHWC')

        conv_input_name = input_names[0]
        conv_output_name = node.name
        builder = self._get_builder()

        if data_format == 'NHWC':
            stride_height = node.attr.get('strides', [1, 1, 1, 1])[1]
            stride_width = node.attr.get('strides', [1, 1, 1, 1])[2]
        else:
            stride_height = node.attr.get('strides', [1, 1, 1, 1])[-2]
            stride_width = node.attr.get('strides', [1, 1, 1, 1])[-1]

        border_mode = node.attr.get('padding').lower()

        layer = builder.add_convolution(
            name=conv_output_name,
            kernel_channels=weight.shape[2],
            output_channels=weight.shape[3],
            height=weight.shape[0],
            width=weight.shape[1],
            stride_height=stride_height,
            stride_width=stride_width,
            border_mode=border_mode,
            groups=1,
            W=weight,
            b=bias,
            has_bias=(bias is not None),
            is_deconv=False,
            output_shape=None,
            input_name=conv_input_name,
            output_name=conv_output_name,
            dilation_factors=[1, 1])

        shapes.propagate_single_layer(layer, self.tensor_shapes, output_shapes=node.attr.get('_output_shapes'))

    def _convert_reshape(self, node):
        input_names = self._get_input_tensors(node)
        if '_output_shapes' in node.attr and node.attr['_output_shapes']:
            output_shape = node.attr['_output_shapes'][0]
            layer = self._get_builder().add_reshape_static(
                name=node.name,
                input_name=input_names[0],
                output_name=node.name,
                output_shape=output_shape)
        else:
            layer = self._get_builder().add_reshape_dynamic(
                name=node.name, input_names=input_names, output_name=node.name)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_argmax(self, node):
        input_names = self._get_input_tensors(node)
        input_shape = self._get_current_graph()[node.inputs[0]].attr['_output_shapes'][0]
        axis = node.attr['reduction_indices'][0]

        layer = self._get_builder().add_argmax(
            name=node.name,
            input_name=input_names[0],
            output_name=node.name,
            axis=axis,
            keepdims=False)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_reverse(self, node):
        input_names = self._get_input_tensors(node)
        inp, axes = input_names[0], input_names[1]
        reverse_axes = self._get_current_graph()[axes].attr['value'].val
        rank = len(self.tensor_shapes[inp])
        reverse_dim = [False] * rank
        for axis in reverse_axes:
            reverse_dim[axis] = True

        layer = self._get_builder().add_reverse(
            name=node.name, input_name=inp, output_name=node.name, reverse_dim=reverse_dim)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_expand_dims(self, node):
        input_names = self._get_input_tensors(node)
        g = self._get_current_graph()
        if len(input_names) == 2 and g[node.inputs[1]].attr['value'].val is None:
            raise NotImplementedError("[SSAConverter] Cannot handle dynamic expandDims")

        axes = g[node.inputs[1]].attr['value'].val
        layer = self._get_builder().add_expand_dims(
            name=node.name, input_name=input_names[0], output_name=node.name, axes=axes)
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_cast(self, node):
        layer = self._get_builder().add_activation(
            name=node.name,
            non_linearity='LINEAR',
            input_name=self._get_input_tensors(node)[0],
            output_name=node.name,
            params=(1.0, 0.0))
        shapes.propagate_single_layer(layer, self.tensor_shapes)

    def _convert_reverse_sequence(self, node):
        raise NotImplementedError('ReverseSequence Not implemented.')

    def _convert_embedding(self, node):
        input_names = self._get_input_tensors(node)
        g = self._get_current_graph()
        weight = None
        if len(input_names) == 1:
            weight = node.attr.get('W')
        elif len(input_names) == 2 and g[input_names[1]].op == 'Const':
            weight = g[input_names[1]].value.val  # (batch, depth, out_channels)

        if weight is None:
            raise ValueError('[SSAConverter] Unable to handle dynamic embedding')

        out_channels = weight.shape[-1]
        depth = node.attr['depth']
        weight = weight.reshape([depth, out_channels]).transpose((1, 0))

        expanddim_name = node.name + '_expandim_'

        layer = self._get_builder().add_expand_dims(
            name=expanddim_name, input_name=input_names[0], output_name=expanddim_name, axes=[-1])
        shapes.propagate_single_layer(layer, self.tensor_shapes)

        layer = self._get_builder().add_embedding_nd(
            name=node.name,
            input_name=expanddim_name,
            output_name=node.name,
            vocab_size=depth,
            embedding_size=out_channels,
            W=weight)
        shapes.propagate_single_layer(layer, self.tensor_shapes)
