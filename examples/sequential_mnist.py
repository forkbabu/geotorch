import torch
import torch.nn as nn
import geotorch
import argparse
from torchvision import datasets, transforms

parser = argparse.ArgumentParser(description="Exponential Layer MNIST Task")
parser.add_argument("--batch_size", type=int, default=128)
parser.add_argument("--hidden_size", type=int, default=170)
parser.add_argument("--epochs", type=int, default=70)
parser.add_argument("--lr", type=float, default=7e-4)
parser.add_argument("--lr_orth", type=float, default=7e-5)
parser.add_argument("--permute", action="store_true")
parser.add_argument(
    "--constraints",
    choices=["orthogonal", "almostorthogonal"],
    default="orthogonal",
    type=str,
)
parser.add_argument(
    "--f", choices=["sigmoid", "tanh", "sin"], default="sigmoid", type=str,
)
parser.add_argument("--r", type=float, default=0.1)


args = parser.parse_args()

# Fix seed across experiments
# Same seed as that used in "Orthogonal Recurrent Neural Networks with Scaled Cayley Transform"
# https://github.com/SpartinStuff/scoRNN/blob/master/scoRNN_copying.py#L79
# torch.backends.cudnn.deterministic = True
# torch.backends.cudnn.benchmark = False
# torch.manual_seed(5544)
# np.random.seed(5544)

n_classes = 10
batch_size = args.batch_size
hidden_size = args.hidden_size
epochs = args.epochs
device = torch.device("cuda")


class modrelu(nn.Module):
    def __init__(self, features):
        # For now we just support square layers
        super(modrelu, self).__init__()
        self.features = features
        self.b = nn.Parameter(torch.Tensor(self.features))
        self.reset_parameters()

    def reset_parameters(self):
        self.b.data.uniform_(-0.01, 0.01)

    def forward(self, inputs):
        norm = torch.abs(inputs)
        biased_norm = norm + self.b
        magnitude = nn.functional.relu(biased_norm)
        phase = torch.sign(inputs)

        return phase * magnitude


class ExpRNNCell(nn.Module):
    def __init__(self, input_size, hidden_size):
        super(ExpRNNCell, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.recurrent_kernel = nn.Linear(hidden_size, hidden_size, bias=False)
        self.input_kernel = nn.Linear(input_size, hidden_size)
        self.nonlinearity = modrelu(hidden_size)

        # Make recurrent_kernel orthogonal
        if args.constraints == "orthogonal":
            geotorch.orthogonal(self.recurrent_kernel, "weight")
        elif args.constraints == "almostorthogonal":
            geotorch.almost_orthogonal(self.recurrent_kernel, "weight", args.r, args.f)
        else:
            raise ValueError("Unexpected constaints. Got {}".format(args.constraints))

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_normal_(self.input_kernel.weight.data, nonlinearity="relu")
        # Initialize the recurrent kernel
        if args.constraints == "orthogonal":
            self.recurrent_kernel.parametrizations.weight.torus_init_()
        elif args.constraints == "almostorthogonal":
            USV = self.recurrent_kernel.parametrizations.weight.total_space
            # Initialise the parts U S V
            USV[0].torus_init_()
            USV[1].base.normal_()
            USV[2].torus_init_()

    def default_hidden(self, input_):
        return input_.new_zeros(input_.size(0), self.hidden_size, requires_grad=False)

    def forward(self, input_, hidden):
        input_ = self.input_kernel(input_)
        hidden = self.recurrent_kernel(hidden)
        out = input_ + hidden
        return self.nonlinearity(out)


class Model(nn.Module):
    def __init__(self, hidden_size, permute):
        super(Model, self).__init__()
        self.permute = permute
        if self.permute:
            self.register_buffer("permutation", torch.randperm(784))
        self.rnn = ExpRNNCell(1, hidden_size)
        self.lin = nn.Linear(hidden_size, n_classes)
        self.loss_func = nn.CrossEntropyLoss()

    def forward(self, inputs):
        if self.permute:
            inputs = inputs[:, self.permutation]
        out_rnn = self.rnn.default_hidden(inputs[:, 0, ...])
        for input in torch.unbind(inputs, dim=1):
            out_rnn = self.rnn(input.unsqueeze(dim=1), out_rnn)
        return self.lin(out_rnn)

    def loss(self, logits, y):
        return self.loss_func(logits, y)

    def correct(self, logits, y):
        return torch.eq(torch.argmax(logits, dim=1), y).float().sum()


def main():
    # Load data
    kwargs = {"num_workers": 1, "pin_memory": True}
    train_loader = torch.utils.data.DataLoader(
        datasets.MNIST(
            "./mnist", train=True, download=True, transform=transforms.ToTensor()
        ),
        batch_size=batch_size,
        shuffle=True,
        **kwargs
    )
    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST("./mnist", train=False, transform=transforms.ToTensor()),
        batch_size=batch_size,
        shuffle=True,
        **kwargs
    )

    # Model and optimizers
    model = Model(hidden_size, args.permute).to(device)
    model.train()

    p_orth = model.rnn.recurrent_kernel
    orth_params = p_orth.parameters()
    non_orth_params = (
        param for param in model.parameters() if param not in set(p_orth.parameters())
    )

    optim = torch.optim.RMSprop(
        [{"params": non_orth_params}, {"params": orth_params, "lr": args.lr_orth}],
        lr=args.lr,
    )

    best_test_acc = 0.0
    for epoch in range(epochs):
        processed = 0
        for batch_idx, (batch_x, batch_y) in enumerate(train_loader):
            batch_x, batch_y = batch_x.to(device).view(-1, 784), batch_y.to(device)

            optim.zero_grad()
            logits = model(batch_x)
            loss = model.loss(logits, batch_y)
            loss.backward()
            optim.step()

            with torch.no_grad():
                correct = model.correct(logits, batch_y)

            processed += len(batch_x)
            print(
                "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}\tAccuracy: {:.2f}%\tBest: {:.2f}%".format(
                    epoch,
                    processed,
                    len(train_loader.dataset),
                    100.0 * batch_idx / len(train_loader),
                    loss.item(),
                    100 * correct / len(batch_x),
                    best_test_acc,
                )
            )

        model.eval()
        with torch.no_grad():
            test_loss = 0.0
            correct = 0.0
            for batch_x, batch_y in test_loader:
                batch_x, batch_y = batch_x.to(device).view(-1, 784), batch_y.to(device)
                logits = model(batch_x)
                test_loss += model.loss(logits, batch_y).float()
                correct += model.correct(logits, batch_y).float()

        test_loss /= len(test_loader)
        test_acc = 100 * correct / len(test_loader.dataset)
        best_test_acc = max(test_acc, best_test_acc)
        print(
            "\nTest set: Average loss: {:.4f}, Accuracy: {:.2f}%, Best Accuracy: {:.2f}%\n".format(
                test_loss, test_acc, best_test_acc
            )
        )

        model.train()


if __name__ == "__main__":
    main()