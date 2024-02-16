import napari
import zarr
import numpy as np
import threading
from qtpy.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QComboBox,
    QHBoxLayout,
    QCheckBox,
    QGroupBox,
    QPushButton,
    QSlider,
    QLineEdit,
)
from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QPainter, QPixmap, QFont
from skimage.feature import multiscale_basic_features
from sklearn.ensemble import RandomForestClassifier
from sklearn.utils.class_weight import compute_class_weight
from skimage import future
import toolz as tz
from psygnal import debounced
from superqt import ensure_main_thread
import logging
import sys
import xgboost as xgb
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
)
from matplotlib.figure import Figure
from magicgui.tqdm import tqdm
from napari.qt.threading import thread_worker
from sklearn.cross_decomposition import PLSRegression
from matplotlib.widgets import LassoSelector
from matplotlib.path import Path
from threading import Thread
from cellcanvas.utils import get_labels_colormap

# from https://github.com/napari/napari/issues/4384

# Define a class to encapsulate the Napari viewer and related functionalities
class CellCanvasApp:
    def __init__(self, zarr_path):
        self.zarr_path = zarr_path
        self.dataset = zarr.open(zarr_path, mode="r")
        self.image_data = self.dataset["crop/original_data"]
        self.feature_data_skimage = self.dataset["features/skimage"]
        self.feature_data_tomotwin = self.dataset["features/tomotwin"]
        self.data_choice = None
        self.corner_pixels = None
        self.model_type = None
        self.prediction_labels = None
        self.prediction_counts = None
        self.painting_labels = None
        self.painting_counts = None        
        self.viewer = napari.Viewer()
        self._add_threading_workers()
        self._init_viewer_layers()
        self._init_logging()
        self._add_widget()
        self.model = None
        self.create_embedding_plot()        

    def _add_threading_workers(self):
        # Model fitting worker
        self.model_fit_worker = None
        # Prediction worker
        self.prediction_worker = None
        self.background_estimation_worker = None        

    def _init_viewer_layers(self):
        self.data_layer = self.viewer.add_image(self.image_data, name="Image", projection_mode='mean')
        self.prediction_data = zarr.open(
            f"{self.zarr_path}/prediction",
            mode="a",
            shape=self.image_data.shape,
            dtype="i4",
            dimension_separator=".",
        )
        self.prediction_layer = self.viewer.add_labels(
            self.prediction_data,
            name="Prediction",
            scale=self.data_layer.scale,
            opacity=0.1,
            color=get_labels_colormap(),
        )
        
        self.painting_data = zarr.open(
            f"{self.zarr_path}/painting",
            mode="a",
            shape=self.image_data.shape,
            dtype="i4",
            dimension_separator=".",
        )
        self.painting_layer = self.viewer.add_labels(
            self.painting_data,
            name="Painting",
            scale=self.data_layer.scale,
            color=get_labels_colormap(),
        )

        self.painting_labels, self.painting_counts = np.unique(self.painting_data[:], return_counts=True)

        # Set defaults for layers
        self.get_painting_layer().brush_size = 2
        self.get_painting_layer().n_edit_dimensions = 3

    def _init_logging(self):
        self.logger = logging.getLogger("cellcanvas")
        self.logger.setLevel(logging.DEBUG)
        streamHandler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        streamHandler.setFormatter(formatter)
        self.logger.addHandler(streamHandler)

    def _add_widget(self):
        self.widget = CellCanvasWidget(self)        
        self.viewer.window.add_dock_widget(self.widget, name="CellCanvas")
        self.widget.estimate_background_button.clicked.connect(
            self.start_background_estimation
        )
        self._connect_events()

    def _connect_events(self):
        # Use a partial function to pass additional arguments to the event handler
        on_data_change_handler = tz.curry(self.on_data_change)(app=self)
        # self.viewer.camera.events, self.viewer.dims.events,
        for listener in [
            self.painting_layer.events.paint,
        ]:
            listener.connect(
                debounced(
                    ensure_main_thread(on_data_change_handler),
                    timeout=1000,
                )
            )

        # TODO other events:
        # - paint layer data change triggers live model fit, class distributions, embeddings
        # - model fit triggers live prediction
        # - prediction triggers class distributions, embeddings

    def get_data_layer(self):
        return self.viewer.layers["Image"]

    def get_prediction_layer(self):
        return self.viewer.layers["Prediction"]

    def get_painting_layer(self):
        return self.viewer.layers["Painting"]

    def on_data_change(self, event, app):
        self.logger.debug("on_data_change")
        # Define corner_pixels based on the current view or other logic
        self.corner_pixels = self.viewer.layers["Image"].corner_pixels        

        self.painting_labels, self.painting_counts = np.unique(self.painting_data[:], return_counts=True)
        
        # Ensure the prediction layer visual is updated
        self.get_prediction_layer().refresh()
        
        # Update class distribution charts
        self.update_class_distribution_charts()

        # Update projection
        self.create_embedding_plot()

        self.widget.setupLegend()

    @thread_worker
    def threaded_on_data_change(
        self,
        event,
        corner_pixels,
        dims,
        model_type,
        feature_params,
        live_fit,
        live_prediction,
        data_choice,
    ):
        self.logger.info(f"Labels data has changed! {event}")

        # Assuming you have a method to prepare features and labels
        features, labels = self.prepare_data_for_model(data_choice, corner_pixels)

        # Update stats
        self.painting_labels, self.painting_counts = np.unique(self.painting_data[:], return_counts=True)

        if live_fit:
            # Pass features and labels along with the model_type
            self.start_model_fit(model_type, features, labels)
        if live_prediction and self.model is not None:
            # For prediction, ensure there's an existing model
            self.features = features
            self.start_prediction()


    def get_model_type(self):
        if not self.model_type:
            self.model_type = self.widget.model_dropdown.currentText()
        return self.model_type
            
    def get_data_choice(self):
        if not self.data_choice:
            self.data_choice = "Whole Image"
        return self.data_choice

    def get_corner_pixels(self):
        if self.corner_pixels is None:
            self.corner_pixels = self.viewer.layers["Image"].corner_pixels        
        return self.corner_pixels
            
    def prepare_data_for_model(self):
        data_choice = self.get_data_choice()
        corner_pixels = self.get_corner_pixels()

        # Find a mask of indices we will use for fetching our data
        mask_idx = (
            slice(
                self.viewer.dims.current_step[0],
                self.viewer.dims.current_step[0] + 1,
            ),
            slice(corner_pixels[0, 1], corner_pixels[1, 1]),
            slice(corner_pixels[0, 2], corner_pixels[1, 2]),
        )
        if data_choice == "Whole Image":
            mask_idx = tuple(
                [slice(0, sz) for sz in self.get_data_layer().data.shape]
            )

        self.logger.info(
            f"mask idx {mask_idx}, image {self.get_data_layer().data.shape}"
        )
        active_image = self.get_data_layer().data[mask_idx]
        self.logger.info(
            f"active image shape {active_image.shape} data choice {data_choice} painting_data {self.painting_data.shape} mask_idx {mask_idx}"
        )

        active_labels = self.painting_data[mask_idx]

        def compute_features(
            mask_idx, use_skimage_features, use_tomotwin_features
        ):
            features = []
            if use_skimage_features:
                features.append(
                    self.feature_data_skimage[mask_idx].reshape(
                        -1, self.feature_data_skimage.shape[-1]
                    )
                )
            if use_tomotwin_features:
                features.append(
                    self.feature_data_tomotwin[mask_idx].reshape(
                        -1, self.feature_data_tomotwin.shape[-1]
                    )
                )

            if features:
                return np.concatenate(features, axis=1)
            else:
                raise ValueError("No features selected for computation.")

        training_labels = None

        use_skimage_features = False
        use_tomotwin_features = True

        if data_choice == "Current Displayed Region":
            # Use only the currently displayed region.
            training_features = compute_features(
                mask_idx, use_skimage_features, use_tomotwin_features
            )
            training_labels = np.squeeze(active_labels)
        elif data_choice == "Whole Image":
            if use_skimage_features:
                training_features = np.array(self.feature_data_skimage)
            else:
                training_features = np.array(self.feature_data_tomotwin)
            training_labels = np.array(self.painting_data)
        else:
            raise ValueError(f"Invalid data choice: {data_choice}")

        if (training_labels is None) or np.any(training_labels.shape == 0):
            self.logger.info("No training data yet. Skipping model update")
            return
        
        return training_features, training_labels

    @thread_worker
    def model_fit_thread(self, model_type, features, labels):
        return self.update_model(labels, features, model_type)
    
    def update_model(self, labels, features, model_type):
        # Flatten labels
        labels = labels.flatten()
        reshaped_features = features.reshape(-1, features.shape[-1])

        # Filter features where labels are greater than 0
        valid_labels = labels > 0
        filtered_features = reshaped_features[valid_labels, :]
        filtered_labels = labels[valid_labels] - 1  # Adjust labels

        if filtered_labels.size == 0:
            self.logger.info("No labels present. Skipping model update.")
            return None

        # Calculate class weights
        unique_labels = np.unique(filtered_labels)
        class_weights = compute_class_weight(
            "balanced", classes=unique_labels, y=filtered_labels
        )
        weight_dict = dict(zip(unique_labels, class_weights))

        # Apply weights
        sample_weights = np.vectorize(weight_dict.get)(filtered_labels)

        # Model fitting
        if model_type == "Random Forest":
            clf = RandomForestClassifier(
                n_estimators=50,
                n_jobs=-1,
                max_depth=10,
                max_samples=0.05,
                class_weight=weight_dict,
            )
            clf.fit(filtered_features, filtered_labels)
            return clf
        elif model_type == "XGBoost":
            clf = xgb.XGBClassifier(
                n_estimators=100, learning_rate=0.1, use_label_encoder=False
            )
            clf.fit(
                filtered_features,
                filtered_labels,
                sample_weight=sample_weights,
            )
            return clf
        else:
            raise ValueError(f"Unsupported model type: {model_type}")

    def predict(self, model, features):
        # We shift labels + 1 because background is 0 and has special meaning
        prediction = (
            future.predict_segmenter(
                features.reshape(-1, features.shape[-1]), model
            ).reshape(features.shape[:-1])
            + 1
        )

        # Compute stats in thread too
        prediction_labels, prediction_counts = np.unique(prediction.data[:], return_counts=True)
        
        return (np.transpose(prediction), prediction_labels, prediction_counts)

    @thread_worker
    def prediction_thread(self, features):
        # The prediction logic
        return self.predict(self.model, features)

    def get_features(self):
        if self.features is None:
            self.features = self.feature_data_tomotwin
        return self.features
    
    def start_prediction(self):
        if self.prediction_worker is not None:
            self.prediction_worker.quit()

        features = self.get_features()
            
        self.prediction_worker = self.prediction_thread(features)
        self.prediction_worker.returned.connect(self.on_prediction_completed)
        self.prediction_worker.start()

    def on_prediction_completed(self, result):
        prediction, prediction_labels, prediction_counts = result
        self.logger.debug("on_prediction_completed")
        self.prediction_data = np.transpose(prediction)

        self.prediction_labels = prediction_labels
        self.prediction_counts = prediction_counts
        
        self.get_prediction_layer().data = self.prediction_data
        self.get_prediction_layer().refresh()

        self.update_class_distribution_charts()
        # self.create_embedding_plot()

    def start_model_fit(self):
        if self.model_fit_worker is not None:
            self.model_fit_worker.quit()

        features, labels = self.prepare_data_for_model()
        self.features = features
        self.labels = labels
            
        self.model_fit_worker = self.model_fit_thread(self.get_model_type(), features, labels)
        self.model_fit_worker.returned.connect(self.on_model_fit_completed)
        # TODO update UI to indicate that model training has started
        self.model_fit_worker.start()

    def on_model_fit_completed(self, model):
        self.logger.debug("on_model_fit_completed")
        self.model = model

        # TODO update UI to indicate model is done training

        if self.widget.live_pred_checkbox.isChecked() and self.model is not None:
            self.logger.debug("live prediction is active, prediction triggered by model fit completion.")
            self.start_prediction(self.prepared_features)        

    def update_class_distribution_charts(self):
        total_pixels = np.product(self.painting_data.shape)

        painting_counts = self.painting_counts
        painting_labels = self.painting_labels
        prediction_counts = self.prediction_counts
        prediction_labels = self.prediction_labels

        # TODO separate painting and prediction paths
        if prediction_labels is None or painting_labels is None:
            return
        
        # Calculate percentages instead of raw counts
        painting_percentages = (painting_counts / total_pixels) * 100
        prediction_percentages = (prediction_counts / total_pixels) * 100

        # Separate subplot for class 0 in painting layer
        unpainted_percentage = painting_percentages[painting_labels == 0] if 0 in painting_labels else [0]

        # Exclude class 0 for prediction layer
        valid_prediction_indices = prediction_labels > 0
        valid_prediction_labels = prediction_labels[valid_prediction_indices]
        valid_prediction_percentages = prediction_percentages[valid_prediction_indices]

        # Exclude class 0 for painting layer percentages
        valid_painting_indices = painting_labels > 0
        valid_painting_labels = painting_labels[valid_painting_indices]
        valid_painting_percentages = painting_percentages[valid_painting_indices]

        # Example class to color mapping
        class_color_mapping = {
            label: "#{:02x}{:02x}{:02x}".format(int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255))
            for label, rgba in get_labels_colormap().items()
        }

        self.widget.figure.clear()

        napari_charcoal_hex = "#262930"

        # Custom style adjustments for dark theme
        dark_background_style = {
            "figure.facecolor": napari_charcoal_hex,
            "axes.facecolor": napari_charcoal_hex,
            "axes.edgecolor": "white",
            "axes.labelcolor": "white",
            "text.color": "white",
            "xtick.color": "white",
            "ytick.color": "white",
            "axes.spines.top": False,
            "axes.spines.right": False,
        }

        with plt.style.context(dark_background_style):
            # Create subplots with adjusted heights
            gs = self.widget.figure.add_gridspec(3, 1, height_ratios=[1, 4, 4])
            ax0 = self.widget.figure.add_subplot(gs[0])
            ax1 = self.widget.figure.add_subplot(gs[1])
            ax2 = self.widget.figure.add_subplot(gs[2])

            # Plot for unpainted pixels
            ax0.barh(0, unpainted_percentage, color="#AAAAAA", edgecolor="white")
            # ax0.set_title("Unpainted", loc='left')
            ax0.set_xlabel("% of Image")
            ax0.set_yticks([])

            # Horizontal bar plots for painting and prediction layers
            ax1.barh(valid_painting_labels, valid_painting_percentages, color=[class_color_mapping.get(x, "#FFFFFF") for x in valid_painting_labels], edgecolor="white")
            # ax1.set_title("Painting", loc='left')

            ax1.set_xlabel("% of Image")
            # ax1.set_yticks(valid_painting_labels)
            ax1.set_yticks([])
            ax1.invert_yaxis()  # Invert y-axis to have labels in ascending order from top to bottom

            ax2.barh(valid_prediction_labels, valid_prediction_percentages, color=[class_color_mapping.get(x, "#FFFFFF") for x in valid_prediction_labels], edgecolor="white")
            # ax2.set_title("Prediction", loc='left')
            ax2.set_xlabel("% of Image")
            # ax2.set_yticks(valid_prediction_labels)
            ax2.set_yticks([])
            ax2.invert_yaxis()

            # Use set_ylabel to position the titles outside and to the left of the y-axis labels
            ax0.set_ylabel("Unpainted", labelpad=20, fontsize=12, rotation=0, ha='right', va='center')
            ax1.set_ylabel("Painting", labelpad=20, fontsize=12, rotation=0, ha='right', va='center')
            ax2.set_ylabel("Prediction", labelpad=20, fontsize=12, rotation=0, ha='right', va='center')

            self.widget.figure.subplots_adjust(left=0.33, right=0.9, top=0.95, bottom=0.05)

        # Adjust the left margin to make space for the y-axis labels (titles)
        plt.subplots_adjust(left=0.25)

        # Automatically adjust subplot params so that the subplot(s) fits into the figure area
        self.widget.figure.tight_layout(pad=3.0)

        # Explicitly set figure background color again to ensure it
        self.widget.figure.patch.set_facecolor(napari_charcoal_hex)

        self.widget.canvas.draw()

    def start_background_estimation(self, model_type, features, labels):
        if self.background_estimation_worker is not None:
            self.background_estimation_worker.quit()

        self.background_estimation_worker = self.estimate_background()
        self.background_estimation_worker.returned.connect(self.on_background_estimation_completed)
        # TODO update UI to indicate that background estimation
        self.background_estimation_worker.start()        
        
    @thread_worker
    def estimate_background(self):
        print("Estimating background label")
        embedding_data = self.feature_data_tomotwin[:]

        # Compute the median of the embeddings
        median_embedding = np.median(embedding_data, axis=(0, 1, 2))

        # Compute the Euclidean distance from the median for each embedding
        distances = np.sqrt(
            np.sum((embedding_data - median_embedding) ** 2, axis=-1)
        )

        # Define a threshold for background detection
        # TODO note this is hardcoded
        threshold = np.percentile(distances.flatten(), 1)

        # Identify background pixels (where distance is less than the threshold)
        background_mask = distances < threshold
        indices = np.where(background_mask)

        print(
            f"Distance distribution: min {np.min(distances)} max {np.max(distances)} mean {np.mean(distances)} median {np.median(distances)} threshold {threshold}"
        )

        print(f"Labeling {np.sum(background_mask)} pixels as background")

        # TODO: optimize this because it is wicked slow
        #       once that is done the threshold can be increased
        # Update the painting data with the background class (1)
        for i in range(len(indices[0])):
            self.painting_data[indices[0][i], indices[1][i], indices[2][i]] = 1

        # Refresh the painting layer to show the updated background
        # self.get_painting_layer().refresh()

    def create_embedding_plot(self):
        self.widget.embedding_figure.clear()

        # Flatten the feature data and labels to match shapes
        features = self.feature_data_tomotwin[:].reshape(-1, self.feature_data_tomotwin.shape[-1])
        labels = self.painting_data[:].flatten()

        # Filter out entries where the label is 0
        filtered_features = features[labels > 0]
        filtered_labels = labels[labels > 0]

        # Check if there are enough samples to proceed
        if filtered_features.shape[0] < 2:
            print("Not enough labeled data to create an embedding plot. Need at least 2 samples.")
            return

        # Proceed with PLSRegression as there's enough data
        self.pls = PLSRegression(n_components=2)
        self.pls_embedding = self.pls.fit_transform(filtered_features, filtered_labels)[0]

        # Original image coordinates
        z_dim, y_dim, x_dim, _ = self.feature_data_tomotwin.shape
        X, Y, Z = np.meshgrid(np.arange(x_dim), np.arange(y_dim), np.arange(z_dim), indexing='ij')
        original_coords = np.vstack([X.ravel(), Y.ravel(), Z.ravel()]).T
        # Filter coordinates using the same mask applied to the features
        self.filtered_coords = original_coords[labels > 0]
        
        
        class_color_mapping = {
            label: "#{:02x}{:02x}{:02x}".format(
                int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255)
            ) for label, rgba in get_labels_colormap().items()
        }

        # Convert filtered_labels to a list of colors for each point
        point_colors = [class_color_mapping[label] for label in filtered_labels]

        # Custom style adjustments for dark theme
        napari_charcoal_hex = "#262930"
        plt.style.use('dark_background')
        self.widget.embedding_figure.patch.set_facecolor(napari_charcoal_hex)

        ax = self.widget.embedding_figure.add_subplot(111, facecolor=napari_charcoal_hex)
        scatter = ax.scatter(self.pls_embedding[:, 0], self.pls_embedding[:, 1], s=0.1, c=point_colors, alpha=1.0)

        plt.setp(ax, xticks=[], yticks=[])
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('white')
        ax.spines['bottom'].set_color('white')

        plt.title('PLS-DA Embedding Using Labels from Painting Layer', color='white')

        def onclick(event):
            if event.inaxes == ax:
                clicked_embedding = np.array([event.xdata, event.ydata])
                distances = np.sqrt(np.sum((self.pls_embedding - clicked_embedding) ** 2, axis=1))
                nearest_point_index = np.argmin(distances)
                nearest_image_coordinates = self.filtered_coords[nearest_point_index]
                print(f"Clicked embedding coordinates: ({event.xdata}, {event.ydata}), Image space coordinate: {nearest_image_coordinates}")

        def onselect(verts):
            path = Path(verts)
            self.update_painting_layer(path)

        # Create the LassoSelector
        self.lasso = LassoSelector(ax, onselect, useblit=True)

        cid = self.widget.embedding_canvas.mpl_connect('button_press_event', onclick)
        self.widget.embedding_canvas.draw()

    def update_painting_layer(self, path):
        # Fetch the currently active label from the painting layer
        target_label = self.get_painting_layer().selected_label
        # Start a new thread to update the painting layer with the current target label
        update_thread = Thread(target=self.paint_thread, args=(path, target_label,))
        update_thread.start()

    def paint_thread(self, lasso_path, target_label):
        # Ensure we're working with the full feature dataset
        all_features_flat = self.feature_data_tomotwin[:].reshape(-1, self.feature_data_tomotwin.shape[-1])

        # Use the PLS model to project these features into the embedding space
        all_embeddings = self.pls.transform(all_features_flat)

        # Determine which points fall within the lasso path
        contained = np.array([lasso_path.contains_point(point) for point in all_embeddings[:, :2]])

        # The shape of the original image data, to map flat indices back to spatial coordinates
        shape = self.feature_data_tomotwin.shape[:-1]

        # Iterate over all points to update the painting data where contained is True
        for idx in np.where(contained)[0]:
            # Map flat index back to spatial coordinates
            z, y, x = np.unravel_index(idx, shape)
            # Update the painting data
            self.painting_data[z, y, x] = target_label

        print(f"Painted {np.sum(contained)} pixels with label {target_label}")
        
        
class CellCanvasWidget(QWidget):
    def __init__(self, app, parent=None):
        super(CellCanvasWidget, self).__init__(parent)
        self.app = app
        self.label_edits = {}
        self.initUI()

    def initUI(self):
        main_layout = QVBoxLayout()

        self.legend_placeholder_index = 0

        # Settings Group
        settings_group = QGroupBox("Settings")
        settings_layout = QVBoxLayout()

        model_layout = QHBoxLayout()
        model_label = QLabel("Select Model")
        self.model_dropdown = QComboBox()
        self.model_dropdown.addItems(["Random Forest", "XGBoost"])
        model_layout.addWidget(model_label)
        model_layout.addWidget(self.model_dropdown)
        settings_layout.addLayout(model_layout)

        self.basic_checkbox = QCheckBox("Basic")
        self.basic_checkbox.setChecked(True)
        settings_layout.addWidget(self.basic_checkbox)

        self.embedding_checkbox = QCheckBox("Embedding")
        self.embedding_checkbox.setChecked(True)
        settings_layout.addWidget(self.embedding_checkbox)

        thickness_layout = QHBoxLayout()
        thickness_label = QLabel("Adjust Slice Thickness")
        self.thickness_slider = QSlider(Qt.Horizontal)
        self.thickness_slider.setMinimum(0)
        self.thickness_slider.setMaximum(50)
        self.thickness_slider.setValue(10)
        thickness_layout.addWidget(thickness_label)
        thickness_layout.addWidget(self.thickness_slider)
        settings_layout.addLayout(thickness_layout)

        settings_group.setLayout(settings_layout)
        main_layout.addWidget(settings_group)

        # Controls Group
        controls_group = QGroupBox("Controls")
        controls_layout = QVBoxLayout()

        # data_layout = QHBoxLayout()
        # data_label = QLabel("Select Data for Model Fitting")
        # self.data_dropdown = QComboBox()
        # self.data_dropdown.addItems(["Current Displayed Region", "Whole Image"])
        # data_layout.addWidget(data_label)
        # data_layout.addWidget(self.data_dropdown)
        # controls_layout.addLayout(data_layout)

        # Live Model Fitting
        live_fit_layout = QHBoxLayout()
        self.live_fit_checkbox = QCheckBox("Live Model Fitting")
        self.live_fit_checkbox.setChecked(False)
        live_fit_button = QPushButton("Fit Model Now")
        live_fit_layout.addWidget(self.live_fit_checkbox)
        live_fit_layout.addWidget(live_fit_button)
        controls_layout.addLayout(live_fit_layout)

        # Live Prediction
        live_pred_layout = QHBoxLayout()
        self.live_pred_checkbox = QCheckBox("Live Prediction")
        self.live_pred_checkbox.setChecked(False)
        live_pred_button = QPushButton("Predict Now")
        live_pred_layout.addWidget(self.live_pred_checkbox)
        live_pred_layout.addWidget(live_pred_button)
        controls_layout.addLayout(live_pred_layout)

        self.estimate_background_button = QPushButton("Estimate Background")
        controls_layout.addWidget(self.estimate_background_button)

        controls_group.setLayout(controls_layout)
        main_layout.addWidget(controls_group)

        # Stats Summary Group
        stats_summary_group = QGroupBox("Stats Summary")
        self.stats_summary_layout = QVBoxLayout()

        self.stats_summary_layout.insertStretch(self.legend_placeholder_index)

        self.setupLegend()
        # Connect legend updates
        try:
            self.app.painting_layer.events.selected_label.connect(self.updateLegendHighlighting)
        except AttributeError:
            # Handle the case where painting_layer or label_changed_signal does not exist
            pass

        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        self.stats_summary_layout.addWidget(self.canvas)

        embedding_label = QLabel("Painting Embedding (Draw to label in embedding)")
        self.stats_summary_layout.addWidget(embedding_label)

        self.embedding_figure = Figure()
        self.embedding_canvas = FigureCanvas(self.embedding_figure)
        self.stats_summary_layout.addWidget(self.embedding_canvas)

        stats_summary_group.setLayout(self.stats_summary_layout)
        main_layout.addWidget(stats_summary_group)

        self.setLayout(main_layout)

        # Connect checkbox signals to actions
        self.live_fit_checkbox.stateChanged.connect(self.on_live_fit_changed)
        self.live_pred_checkbox.stateChanged.connect(self.on_live_pred_changed)

        # Connect button clicks to actions
        live_fit_button.clicked.connect(self.app.start_model_fit)
        live_pred_button.clicked.connect(self.app.start_prediction)

        self.thickness_slider.valueChanged.connect(self.on_thickness_changed)

    def on_live_fit_changed(self, state):
        if state == Qt.Checked:
            self.app.start_model_fit()

    def on_live_pred_changed(self, state):
        if state == Qt.Checked:
            # TODO might need to check if this is safe to do, e.g. if a model exists
            self.app.start_prediction()

    def fit_model_now(self):
        self.app.start_model_fit()

    def predict_now(self):
        self.app.start_prediction()

    def setupLegend(self):
        if not hasattr(self, 'class_labels_mapping'):
            # Initialize class labels
            self.class_labels_mapping = {}

        if hasattr(self, 'legend_group'):
            self.stats_summary_layout.takeAt(self.legend_placeholder_index).widget().deleteLater()

        painting_layer = self.app.get_painting_layer()
        self.legend_layout = QVBoxLayout()
        self.legend_group = QGroupBox("Class Labels Legend")
        # Track label edits
        self.label_edits = {}

        active_labels = self.app.painting_labels

        if active_labels is not None:
            for label_id in active_labels:
                color = painting_layer.color.get(label_id)

                # Create a QLabel for color swatch
                color_swatch = QLabel()
                pixmap = QPixmap(16, 16)

                if color is None:
                    pixmap = self.createCheckerboardPattern()
                else:
                    pixmap.fill(QColor(*[int(c * 255) for c in color]))

                color_swatch.setPixmap(pixmap)

                # Update the mapping with new classes or use the existing name
                if label_id not in self.class_labels_mapping:
                    self.class_labels_mapping[label_id] = f"Class {label_id if label_id is not None else 0}"

                # Use the name from the mapping
                label_name = self.class_labels_mapping[label_id]
                label_edit = QLineEdit(label_name)

                # Highlight the label if it is currently being used
                if label_id == painting_layer._selected_label:
                    self.highlightLabel(label_edit)

                # Save changes to class labels back to the mapping
                label_edit.textChanged.connect(lambda text, id=label_id: self.updateClassLabelName(id, text))

                # Layout for each legend entry
                entry_layout = QHBoxLayout()
                entry_layout.addWidget(color_swatch)
                entry_layout.addWidget(label_edit)
                self.legend_layout.addLayout(entry_layout)
                self.label_edits[label_id] = label_edit

        self.legend_group.setLayout(self.legend_layout)
        self.stats_summary_layout.insertWidget(self.legend_placeholder_index, self.legend_group)

    def updateLegendHighlighting(self, selected_label_event):
        """Update highlighting of legend"""
        current_label_id = selected_label_event.source._selected_label

        for label_id, label_edit in self.label_edits.items():
            if label_id == current_label_id:
                self.highlightLabel(label_edit)
            else:
                self.removeHighlightLabel(label_edit)
        
    def highlightLabel(self, label_edit):
        label_edit.setStyleSheet("QLineEdit { background-color: #3D6A88; }")

    def removeHighlightLabel(self, label_edit):
        label_edit.setStyleSheet("")        

    def updateClassLabelName(self, label_id, name):
        self.class_labels_mapping[label_id] = name

    def createCheckerboardPattern(self):
        """Creates a QPixmap with a checkerboard pattern."""
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.white)
        painter = QPainter(pixmap)
        painter.setPen(Qt.NoPen)
        
        # Define the colors for the checkerboard squares
        color1 = Qt.lightGray
        color2 = Qt.darkGray
        size = 4

        for x in range(0, pixmap.width(), size):
            for y in range(0, pixmap.height(), size):
                if (x + y) // size % 2 == 0:
                    painter.fillRect(x, y, size, size, color1)
                else:
                    painter.fillRect(x, y, size, size, color2)

        painter.end()
        return pixmap
        
    def on_thickness_changed(self, value):
        self.app.viewer.dims.thickness = (value, ) * self.app.viewer.dims.ndim


# Initialize your application
if __name__ == "__main__":
    # zarr_path = "/Users/kharrington/Data/CryoCanvas/cryocanvas_crop_007.zarr"
    # zarr_path = "/Users/kharrington/Data/CryoCanvas/cryocanvas_crop_007_v2.zarr/cryocanvas_crop_007.zarr"
    zarr_path = "/Users/kharrington/Data/cellcanvas/cellcanvas_crop_007.zarr/"
    # zarr_path = "/Users/kharrington/Data/cellcanvas/cellcanvas_crop_009.zarr/"
    # zarr_path = "/Users/kharrington/Data/cellcanvas/cellcanvas_crop_010.zarr/"
    app = CellCanvasApp(zarr_path)
    # napari.run()

# TODOs:
# - separate compute from figure generation
# - 
